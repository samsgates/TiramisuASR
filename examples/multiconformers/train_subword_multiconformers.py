# Copyright 2020 Huy Le Nguyen (@usimarit) and Huy Phan (@pquochuy)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import math
import argparse
from tiramisu_asr.utils import setup_environment, setup_strategy

setup_environment()
import tensorflow as tf

DEFAULT_YAML = os.path.join(os.path.abspath(os.path.dirname(__file__)), "config.yml")

tf.keras.backend.clear_session()

parser = argparse.ArgumentParser(prog="MultiConformers Training")

parser.add_argument("--config", type=str, default=DEFAULT_YAML,
                    help="The file path of model configuration file")

parser.add_argument("--max_ckpts", type=int, default=10,
                    help="Max number of checkpoints to keep")

parser.add_argument("--tfrecords", default=False, action="store_true",
                    help="Whether to use tfrecords")

parser.add_argument("--nfx", default=False, action="store_true",
                    help="Whether to use numpy feature extraction")

parser.add_argument("--tbs", type=int, default=None,
                    help="Train batch size per replicas")

parser.add_argument("--ebs", type=int, default=None,
                    help="Evaluation batch size per replicas")

parser.add_argument("--devices", type=int, nargs="*", default=[0],
                    help="Devices' ids to apply distributed training")

parser.add_argument("--mxp", default=False, action="store_true",
                    help="Enable mixed precision")

parser.add_argument("--cache", default=False, action="store_true",
                    help="Enable caching for dataset")

parser.add_argument("--subwords_prefix", type=str, default=None,
                    help="Prefix of file that stores generated subwords")

parser.add_argument("--subwords_corpus", nargs="*", type=str, default=[],
                    help="Transcript files for generating subwords")

args = parser.parse_args()

tf.config.optimizer.set_experimental_options({"auto_mixed_precision": args.mxp})

strategy = setup_strategy(args.devices)

from tiramisu_asr.configs.user_config import UserConfig
from tiramisu_asr.models.multiconformers import MultiConformers
from tiramisu_asr.featurizers.speech_featurizers import TFSpeechFeaturizer
from tiramisu_asr.featurizers.speech_featurizers import NumpySpeechFeaturizer
from tiramisu_asr.featurizers.text_featurizers import SubwordFeaturizer
from tiramisu_asr.optimizers.schedules import TransformerSchedule

from multiconformers_trainer import MultiConformersTrainer
from multiconformers_dataset import MultiConformersTFRecordDataset, MultiConformersSliceDataset

config = UserConfig(DEFAULT_YAML, args.config, learning=True)
lms_config = config["speech_config"]
lms_config["feature_type"] = "log_mel_spectrogram"
lgs_config = config["speech_config"]
lgs_config["feature_type"] = "log_gammatone_spectrogram"

if args.nfx:
    speech_featurizer_lms = NumpySpeechFeaturizer(lms_config)
    speech_featurizer_lgs = NumpySpeechFeaturizer(lgs_config)
else:
    speech_featurizer_lms = TFSpeechFeaturizer(lms_config)
    speech_featurizer_lgs = TFSpeechFeaturizer(lgs_config)

if args.subwords_prefix and os.path.exists(f"{args.subwords_prefix}.subwords"):
    print("Loading subwords ...")
    text_featurizer = SubwordFeaturizer.load_from_file(config["decoder_config"],
                                                       args.subwords_prefix)
else:
    print("Generating subwords ...")
    text_featurizer = SubwordFeaturizer.build_from_corpus(
        config["decoder_config"],
        corpus_files=args.subwords_corpus
    )
    text_featurizer.subwords.save_to_file(args.subwords_prefix)

if args.tfrecords:
    train_dataset = MultiConformersTFRecordDataset(
        data_paths=config["learning_config"]["dataset_config"]["train_paths"],
        tfrecords_dir=config["learning_config"]["dataset_config"]["tfrecords_dir"],
        speech_featurizer_lms=speech_featurizer_lms,
        speech_featurizer_lgs=speech_featurizer_lgs,
        text_featurizer=text_featurizer, stage="train",
        augmentations=config["learning_config"]["augmentations"],
        cache=args.cache, shuffle=True,
    )
    eval_dataset = MultiConformersTFRecordDataset(
        data_paths=config["learning_config"]["dataset_config"]["eval_paths"],
        tfrecords_dir=config["learning_config"]["dataset_config"]["tfrecords_dir"],
        speech_featurizer_lms=speech_featurizer_lms,
        speech_featurizer_lgs=speech_featurizer_lgs,
        text_featurizer=text_featurizer,
        stage="eval", cache=args.cache, shuffle=True
    )
else:
    train_dataset = MultiConformersSliceDataset(
        speech_featurizer_lms=speech_featurizer_lms,
        speech_featurizer_lgs=speech_featurizer_lgs,
        text_featurizer=text_featurizer,
        data_paths=config["learning_config"]["dataset_config"]["train_paths"],
        augmentations=config["learning_config"]["augmentations"],
        stage="train", cache=args.cache, shuffle=True
    )
    eval_dataset = MultiConformersSliceDataset(
        speech_featurizer_lms=speech_featurizer_lms,
        speech_featurizer_lgs=speech_featurizer_lgs,
        text_featurizer=text_featurizer,
        data_paths=config["learning_config"]["dataset_config"]["eval_paths"],
        stage="eval", cache=args.cache, shuffle=True
    )

multiconformers_trainer = MultiConformersTrainer(
    config=config["learning_config"]["running_config"],
    text_featurizer=text_featurizer, strategy=strategy
)

with multiconformers_trainer.strategy.scope():
    multiconformers = MultiConformers(
        **config["model_config"],
        vocabulary_size=text_featurizer.num_classes
    )
    multiconformers._build(speech_featurizer_lms.shape, speech_featurizer_lgs.shape)
    multiconformers.summary(line_length=120)

    optimizer_config = config["learning_config"]["optimizer_config"]
    optimizer = tf.keras.optimizers.Adam(
        TransformerSchedule(
            d_model=config["model_config"]["dmodel"],
            warmup_steps=optimizer_config["warmup_steps"],
            max_lr=(0.05 / math.sqrt(config["model_config"]["dmodel"]))
        ),
        beta_1=optimizer_config["beta1"],
        beta_2=optimizer_config["beta2"],
        epsilon=optimizer_config["epsilon"]
    )

multiconformers_trainer.compile(model=multiconformers, optimizer=optimizer,
                                max_to_keep=args.max_ckpts)

multiconformers_trainer.fit(config["learning_config"]["gradpolicy"],
                            train_dataset, eval_dataset, train_bs=args.tbs, eval_bs=args.ebs)
