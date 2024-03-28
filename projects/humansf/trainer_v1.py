"""

TESTING:
JAX_TRACEBACK_FILTERING=off python -m ipdb -c continue projects/humansf/trainer_v1.py \
  --debug=True \
  --wandb=False \
  --search=default

JAX_DISABLE_JIT=1 JAX_TRACEBACK_FILTERING=off python -m ipdb -c continue projects/humansf/trainer_v1.py \
  --debug=True \
  --wandb=False \
  --search=default

TESTING SLURM LAUNCH:
python projects/humansf/trainer_v1.py \
  --parallel=sbatch \
  --debug_parallel=True \
  --search=default

RUNNING ON SLURM:
python projects/humansf/trainer_v1.py \
  --parallel=sbatch \
  --search=default
"""
from typing import Dict, Union

from absl import flags
from absl import app

import os
import jax
import json
import functools

from ray import tune

import wandb
from safetensors.flax import save_file
from flax.traverse_util import flatten_dict

import hydra
import gymnax
from gymnax.wrappers.purerl import FlattenObservationWrapper, LogWrapper
from library.wrappers import TimestepWrapper

os.environ['XLA_PYTHON_CLIENT_PREALLOCATE']='false'

import library.flags
from library import parallel
from projects.humansf import qlearning
from projects.humansf import keyroom
from projects.humansf.minigrid_common import AutoResetWrapper
from singleagent import value_based_basics as vbb

FLAGS = flags.FLAGS

def run_single(
        sweep_config: dict,
        config_path: str,
        save_path: str = None):

    config, wandb_init = parallel.load_hydra_config(
        sweep_config,
        config_path=config_path,
        save_path=save_path,
        tags=[f"jax_{jax.__version__}"]
        )
    wandb.init(**wandb_init)

    # Open the file and load the JSON data
    maze_path = os.path.join('projects/humansf', "maze_pairs.json")
    with open(maze_path, "r") as file:
      maze_config = json.load(file)[0]

    num_rooms = config['env'].get('NUM_ROOMS', 4)
    maze_config = keyroom.shorten_maze_config(
       maze_config, num_rooms)

    env = keyroom.KeyRoom()
    env_params = env.default_params(maze_config=maze_config)
    test_env_params = env_params.replace(
       training=False
    )

    # auto-reset wrapper
    env = AutoResetWrapper(env)

    alg_name = config['alg']
    if alg_name == 'qlearning':
      make_train = functools.partial(
          vbb.make_train,
          make_agent=qlearning.make_agent,
          make_optimizer=qlearning.make_optimizer,
          make_loss_fn_class=qlearning.make_loss_fn_class,
          make_actor=qlearning.make_actor,
      )
    elif alg_name == 'qlearning_step':
      make_train = functools.partial(
          vbb.make_train,
          make_agent=qlearning.make_agent,
          make_optimizer=qlearning.make_optimizer,
          make_loss_fn_class=qlearning.make_loss_fn_class,
          make_actor=qlearning.make_actor,
          train_step_type='step',
      )
    else:
      raise NotImplementedError(alg_name)

    train_fn = make_train(
      config=config,
      env=env,
      env_params=env_params,
      test_env_params=test_env_params)
    train_vjit = jax.jit(jax.vmap(train_fn))

    rng = jax.random.PRNGKey(config["SEED"])
    rngs = jax.random.split(rng, config["NUM_SEEDS"])
    outs = jax.block_until_ready(train_vjit(rngs))

    if save_path is not None:
        def save_params(params: Dict, filename: Union[str, os.PathLike]) -> None:
            flattened_dict = flatten_dict(params, sep=',')
            save_file(flattened_dict, filename)

        model_state = outs['runner_state'][0]
        params = jax.tree_map(lambda x: x[0], model_state.params) # save only params of the firt run
        os.makedirs(save_path, exist_ok=True)
        save_params(params, f'{save_path}/{alg_name}.safetensors')
        print(f'Parameters of first batch saved in {save_path}/{alg_name}.safetensors')

def sweep(search: str = ''):
  search = search or 'default'
  if search == 'default':
    space = [
        {
            "group": tune.grid_search(['run-5-qlearning']),
            "alg": tune.grid_search(['qlearning_step', 'qlearning']),
            "config_name": tune.grid_search(['qlearning']),
            "AGENT_HIDDEN_DIM": tune.grid_search([64, 128]),
            "AGENT_INIT_SCALE": tune.grid_search([2., .1]),
        }
    ]
  else:
    raise NotImplementedError(search)

  return space

def main(_):
  parallel.run(
      trainer_filename=__file__,
      config_path='projects/humansf/configs',
      run_fn=run_single,
      sweep_fn=sweep,
      folder=os.environ.get(
          'RL_RESULTS_DIR', '/tmp/rl_results_dir')
  )

if __name__ == '__main__':
  app.run(main)