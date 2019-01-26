#!/usr/bin/env python3

import argparse
from sklearn.metrics import confusion_matrix
import data
import datetime
import kmodel      # TODO: rename back to model
import numpy as np
import os
import sys
import tensorflow as tf
import tensorflow.contrib.slim as slim
import util as u
import test
import time
from scipy.special import expit
import json

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--train-image-dir', type=str, default="sample_data/training/", help="training images")
parser.add_argument('--test-image-dir', type=str, default="sample_data/test/", help="test images")
parser.add_argument('--label-dir', type=str, default="sample_data/labels/", help="labels for train/test")
parser.add_argument('--label-db', type=str, default="label.201802_sample.db",
                    help="label_db for test P/R/F1 stats")
parser.add_argument('--patch-width-height', type=int, default=None,
                    help="what size square patches to sample. None => no patch, i.e. use full res image")
parser.add_argument('--batch-size', type=int, default=32, help=' ')
parser.add_argument('--learning-rate', type=float, default=0.001, help=' ')
parser.add_argument('--pos-weight', type=float, default=1.0, help='positive class weight in loss. 1.0 = balanced')
parser.add_argument('--run', type=str, required=True, help="run dir for tb & ckpts")
parser.add_argument('--no-use-skip-connections', action='store_true', help='set to disable skip connections')
parser.add_argument('--no-use-batch-norm', action='store_true', help='set to disable batch norm')
parser.add_argument('--base-filter-size', type=int, default=8, help=' ')
parser.add_argument('--flip-left-right', action='store_true', help='randomly flip training egs left/right')
parser.add_argument('--random-rotate', action='store_true', help='randomly rotate training images')
parser.add_argument('--steps', type=int, default=100000, help='max number of training steps (summaries every --train-steps)')
parser.add_argument('--train-steps', type=int, default=100, help='number training steps between test and summaries')
parser.add_argument('--secs', type=int, default=None, help='If set, max number of seconds to run')
parser.add_argument('--width', type=int, default=None, help='test input image width')
parser.add_argument('--height', type=int, default=None, help='test input image height')
parser.add_argument('--connected-components-threshold', type=float, default=0.05)
opts = parser.parse_args()
print("opts %s" % opts, file=sys.stderr)

# prep ckpt dir (and save off opts)
ckpt_dir = "ckpts/%s" % opts.run
if not os.path.exists(ckpt_dir):
  os.makedirs(ckpt_dir)
with open("%s/opts.json" % ckpt_dir, "w") as f:
  f.write(json.dumps(vars(opts)))

np.set_printoptions(precision=2, threshold=10000, suppress=True, linewidth=10000)

#from tensorflow.python import debug as tf_debug
#tf.keras.backend.set_session(tf_debug.LocalCLIDebugWrapperSession(tf.Session()))

# Build readers / model for training
train_imgs_xys_bitmaps = data.img_xys_iterator(image_dir=opts.train_image_dir,
                                               label_dir=opts.label_dir,
                                               batch_size=opts.batch_size,
                                               patch_width_height=opts.patch_width_height,
                                               distort_rgb=True,
                                               flip_left_right=opts.flip_left_right,
                                               random_rotation=opts.random_rotate,
                                               repeat=True,
                                               width=opts.width, height=opts.height)

# TODO: need to inspect dataset to see how many images there are so (N) that we know
#       when batch is B we should do model.evaluate(steps=N/B)
# TODO: could do all these calcs in test.pr_stats (rather than iterating twice)
test_imgs_xys_bitmaps = data.img_xys_iterator(image_dir=opts.test_image_dir,
                                              label_dir=opts.label_dir,
                                              batch_size=opts.batch_size,
                                              patch_width_height=opts.patch_width_height,
                                              distort_rgb=False,
                                              flip_left_right=False,
                                              random_rotation=False,
                                              repeat=False,
                                              width=opts.width, height=opts.height)

num_test_files = len(os.listdir(opts.test_image_dir))
num_test_steps = num_test_files // opts.batch_size
print("num_test_files=", num_test_files, "batch_size=", opts.batch_size, "=> num_test_steps=", num_test_steps)

# training model might be patch, or full res
train_model = kmodel.construct_model(width=opts.patch_width_height or opts.width,
                                     height=opts.patch_width_height or opts.height,
                                     use_skip_connections=not opts.no_use_skip_connections,
                                     base_filter_size=opts.base_filter_size,
                                     use_batch_norm=not opts.no_use_batch_norm)
kmodel.compile_model(train_model,
                     learning_rate=opts.learning_rate,
                     pos_weight=opts.pos_weight)
print("TRAIN MODEL")
print(train_model.summary())

# always build test model in full res
test_model =  kmodel.construct_model(width=opts.width,
                                     height=opts.height,
                                     use_skip_connections=not opts.no_use_skip_connections,
                                     base_filter_size=opts.base_filter_size,
                                     use_batch_norm=not opts.no_use_batch_norm)
print("TEST MODEL")
print(test_model.summary())

# Setup summary writers. (Will create explicit summaries to write)
# TODO: include keras default callback
train_summaries_writer = tf.summary.FileWriter("tb/%s/training" % opts.run, None)
test_summaries_writer = tf.summary.FileWriter("tb/%s/test" % opts.run, None)

start_time = time.time()
done = False
step = 0

while not done:

  # train a bit.
  history = train_model.fit(train_imgs_xys_bitmaps,
                            epochs=1, verbose=0,
                            steps_per_epoch=opts.train_steps)
  train_loss = history.history['loss'][0]

  # do eval using test model
  # TODO: switch to sharing layers between these two over this explicit get/set_weights
  test_model.set_weights(train_model.get_weights())
  test_loss = train_model.evaluate(test_imgs_xys_bitmaps,
                                   steps=num_test_steps)

  # report one liner
  print("step %d/%d\ttime %d\ttrain_loss %f\ttest_loss %f" % (step, opts.steps,
                                                              int(time.time()-start_time),
                                                              train_loss, test_loss))

  # train / test summaries
  # includes loss summaries as well as a hand rolled debug image

  # ...train
  # TODO: best way to integrate debug_img for test (?)
  #       (i.e. how to tap an element from fit())
  train_summaries_writer.add_summary(u.explicit_summaries({"xent": train_loss}), step)
#  debug_img_summary = u.pil_image_to_tf_summary(u.debug_img(i[0], bm[0], o[0]))
#  train_summaries_writer.add_summary(debug_img_summary, step)
  train_summaries_writer.flush()

  # save model
  dts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
  save_filename = "%s/%s" % (ckpt_dir, dts)
  print("save_filename", save_filename)
  train_model.save_weights(save_filename)
#  train_model.save(save_filename)

  # ... test
  # TODO: here we are reloading model from scratch, would be quicker to use a shared layers model
  stats = test.pr_stats(opts.run, opts.test_image_dir, opts.label_db, opts.connected_components_threshold)
  print("test stats", stats)
  tag_values = {k: stats[k] for k in ['precision', 'recall', 'f1']}
  test_summaries_writer.add_summary(u.explicit_summaries({"xent": test_loss}), step)
  test_summaries_writer.add_summary(u.explicit_summaries(tag_values), step)
  debug_img_summary = u.pil_image_to_tf_summary(stats['debug_img'])
  test_summaries_writer.add_summary(debug_img_summary, step)
  test_summaries_writer.flush()

  # check if done by steps or time
  step += 1  # TODO: fetch global_step from keras model (?)
  if step >= opts.steps:
    done = True
  if opts.secs is not None:
    run_time = time.time() - start_time
    remaining_time = opts.secs - run_time
    print("run_time %s remaining_time %s" % (u.hms(run_time), u.hms(remaining_time)))
    if remaining_time < 0:
      done = True
