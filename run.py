# Copyright 2019 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Main function to run the code."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os
import numpy as np
from src.data_provider import datasets_factory
from src.models.model_factory import Model
import src.trainer as trainer
from src.utils import preprocess
import tensorflow.compat.v1 as tf
tf.disable_v2_behavior()

# -----------------------------------------------------------------------------
FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('train_data_paths', '', 'train data paths.')
tf.app.flags.DEFINE_string('valid_data_paths', '', 'validation data paths.')
tf.app.flags.DEFINE_string('save_dir', '', 'dir to store trained net.')
tf.app.flags.DEFINE_string('gen_frm_dir', '', 'dir to store result.')

tf.app.flags.DEFINE_boolean('is_training', True, 'training or testing')
tf.app.flags.DEFINE_string('dataset_name', 'mnist', 'The name of dataset.')
tf.app.flags.DEFINE_integer('input_length', 10, 'input length.')
tf.app.flags.DEFINE_integer('total_length', 20, 'total input and output length.')
tf.app.flags.DEFINE_integer('img_width', 64, 'input image width.')
tf.app.flags.DEFINE_integer('img_channel', 1, 'number of image channel.')
tf.app.flags.DEFINE_integer('patch_size', 1, 'patch size on one dimension.')
tf.app.flags.DEFINE_boolean('reverse_input', False,
                     'reverse the input/outputs during training.')

tf.app.flags.DEFINE_string('model_name', 'e3d_lstm', 'The name of the architecture.')
tf.app.flags.DEFINE_string('pretrained_model', '', '.ckpt file to initialize from.')
tf.app.flags.DEFINE_string('num_hidden', '64,64,64,64',
                    'COMMA separated number of units of e3d lstms.')
tf.app.flags.DEFINE_integer('filter_size', 5, 'filter of a e3d lstm layer.')
tf.app.flags.DEFINE_boolean('layer_norm', True, 'whether to apply tensor layer norm.')

tf.app.flags.DEFINE_boolean('scheduled_sampling', True, 'for scheduled sampling')
tf.app.flags.DEFINE_integer('sampling_stop_iter', 50000, 'for scheduled sampling.')
tf.app.flags.DEFINE_float('sampling_start_value', 1.0, 'for scheduled sampling.')
tf.app.flags.DEFINE_float('sampling_changing_rate', 0.00002, 'for scheduled sampling.')

tf.app.flags.DEFINE_float('lr', 0.001, 'learning rate.')
tf.app.flags.DEFINE_integer('batch_size', 8, 'batch size for training.')
tf.app.flags.DEFINE_integer('max_iterations', 80000, 'max num of steps.')
tf.app.flags.DEFINE_integer('display_interval', 1,
                     'number of iters showing training loss.')
tf.app.flags.DEFINE_integer('test_interval', 1000, 'number of iters for test.')
tf.app.flags.DEFINE_integer('snapshot_interval', 1000,
                     'number of iters saving models.')
tf.app.flags.DEFINE_integer('num_save_samples', 10, 'number of sequences to be saved.')
tf.app.flags.DEFINE_integer('n_gpu', 1,
                     'how many GPUs to distribute the training across.')
tf.app.flags.DEFINE_boolean('allow_gpu_growth', True, 'allow gpu growth')


def main(_):
  """Main function."""
  # print(FLAGS.reverse_input)
  if tf.gfile.Exists(FLAGS.save_dir):
    tf.gfile.DeleteRecursively(FLAGS.save_dir)
  tf.gfile.MakeDirs(FLAGS.save_dir)
  if tf.gfile.Exists(FLAGS.gen_frm_dir):
    tf.gfile.DeleteRecursively(FLAGS.gen_frm_dir)
  tf.gfile.MakeDirs(FLAGS.gen_frm_dir)

  gpu_list = np.asarray(
      os.environ.get('CUDA_VISIBLE_DEVICES', '-1').split(','), dtype=np.int32)
  FLAGS.n_gpu = len(gpu_list)
  print('Initializing models')

  model = Model(FLAGS)

  if FLAGS.is_training:
    train_wrapper(model)
  else:
    test_wrapper(model)


def schedule_sampling(eta, itr):
  """Gets schedule sampling parameters for training."""
  zeros = np.zeros(
      (FLAGS.batch_size, FLAGS.total_length - FLAGS.input_length - 1,
       FLAGS.img_width // FLAGS.patch_size, FLAGS.img_width // FLAGS.patch_size,
       FLAGS.patch_size**2 * FLAGS.img_channel))
  if not FLAGS.scheduled_sampling:
    return 0.0, zeros

  if itr < FLAGS.sampling_stop_iter:
    eta -= FLAGS.sampling_changing_rate
  else:
    eta = 0.0
  random_flip = np.random.random_sample(
      (FLAGS.batch_size, FLAGS.total_length - FLAGS.input_length - 1))
  true_token = (random_flip < eta)
  ones = np.ones(
      (FLAGS.img_width // FLAGS.patch_size, FLAGS.img_width // FLAGS.patch_size,
       FLAGS.patch_size**2 * FLAGS.img_channel))
  zeros = np.zeros(
      (FLAGS.img_width // FLAGS.patch_size, FLAGS.img_width // FLAGS.patch_size,
       FLAGS.patch_size**2 * FLAGS.img_channel))
  real_input_flag = []
  for i in range(FLAGS.batch_size):
    for j in range(FLAGS.total_length - FLAGS.input_length - 1):
      if true_token[i, j]:
        real_input_flag.append(ones)
      else:
        real_input_flag.append(zeros)
  real_input_flag = np.array(real_input_flag)
  real_input_flag = np.reshape(
      real_input_flag,
      (FLAGS.batch_size, FLAGS.total_length - FLAGS.input_length - 1,
       FLAGS.img_width // FLAGS.patch_size, FLAGS.img_width // FLAGS.patch_size,
       FLAGS.patch_size**2 * FLAGS.img_channel))
  return eta, real_input_flag


def train_wrapper(model):
  """Wrapping function to train the model."""
  if FLAGS.pretrained_model:
    model.load(FLAGS.pretrained_model)
  # load data
  train_input_handle, test_input_handle = datasets_factory.data_provider(
      FLAGS.dataset_name,
      FLAGS.train_data_paths,
      FLAGS.valid_data_paths,
      FLAGS.batch_size * FLAGS.n_gpu,
      FLAGS.img_width,
      seq_length=FLAGS.total_length,
      is_training=True)

  eta = FLAGS.sampling_start_value

  for itr in range(1, FLAGS.max_iterations + 1):
    if train_input_handle.no_batch_left():
      train_input_handle.begin(do_shuffle=True)
    ims = train_input_handle.get_batch()
    if FLAGS.dataset_name == 'penn':
      ims = ims['frame']
    ims = preprocess.reshape_patch(ims, FLAGS.patch_size)

    eta, real_input_flag = schedule_sampling(eta, itr)

    trainer.train(model, ims, real_input_flag, FLAGS, itr)

    if itr % FLAGS.snapshot_interval == 0:
      model.save(itr)

    if itr % FLAGS.test_interval == 0:
      trainer.test(model, test_input_handle, FLAGS, itr)

    train_input_handle.next()


def test_wrapper(model):
  model.load(FLAGS.pretrained_model)
  test_input_handle = datasets_factory.data_provider(
      FLAGS.dataset_name,
      FLAGS.train_data_paths,
      FLAGS.valid_data_paths,
      FLAGS.batch_size * FLAGS.n_gpu,
      FLAGS.img_width,
      is_training=False)
  trainer.test(model, test_input_handle, FLAGS, 'test_result')


if __name__ == '__main__':
  tf.app.run()
