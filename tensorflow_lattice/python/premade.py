# Copyright 2019 Google LLC
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
"""TF Lattice premade models implement typical monotonic model architectures.

You can use TFL premade models to easily construct commonly used monotonic model
architectures. To construct a TFL premade model, construct a model configuration
from `tfl.configs` and pass it to the premade model constructor. No fields in
the model config will be automatically filled in, so the config must be fully
specified. Note that the inputs to the model should match the order in which
they are defined in the feature configs.

```python
model_config = tfl.configs.CalibratedLatticeConfig(...)
calibrated_lattice_model = tfl.premade.CalibratedLattice(
    model_config=model_config)
calibrated_lattice_model.compile(...)
calibrated_lattice_model.fit(...)
```

Supported models are defined in `tfl.configs`. Each model architecture can be
used the same as any other `tf.keras.Model`.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from . import aggregation_layer
from . import categorical_calibration_layer
from . import configs
from . import lattice_layer
from . import linear_layer
from . import parallel_combination_layer
from . import premade_lib
from . import pwl_calibration_layer

from absl import logging
import tensorflow as tf


# TODO: add support for serialization and object scoping or annoations.
class CalibratedLatticeEnsemble(tf.keras.Model):
  """Premade model for Tensorflow calibrated lattice ensemble models.

  Creates a `tf.keras.Model` for the model architecture specified by the
  `model_config`, which should be a
  `tfl.configs.CalibratedLatticeEnsembleConfig`. No fields in the model config
  will be automatically filled in, so the config must be fully specified. Note
  that the inputs to the model should match the order in which they are defined
  in the feature configs.

  Example:

  ```python
  model_config = tfl.configs.CalibratedLatticeEnsembleConfig(...)
  calibrated_lattice_ensemble_model = tfl.premade.CalibratedLatticeEnsemble(
      model_config=model_config)
  calibrated_lattice_ensemble_model.compile(...)
  calibrated_lattice_ensemble_model.fit(...)
  ```

  Attributes:
    model_config: Model configuration object describing model architecture.
      Should be a `tfl.configs.CalibratedLatticeEnsembleConfig` instance.
  """

  def __init__(self, model_config=None, dtype=tf.float32, **kwargs):
    """Initializes a `CalibratedLatticeEnsemble` instance.

    Args:
      model_config: Model configuration object describing model architecutre.
        Should be one of the model configs in `tfl.configs`.
      dtype: dtype of layers used in the model.
      **kwargs: Any additional `tf.keras.Model` arguments
    """
    # Set our model_config
    self.model_config = model_config
    # Check if we are constructing with already provided inputs/outputs, e.g.
    # when we are loading a model.
    if 'inputs' in kwargs and 'outputs' in kwargs:
      super(CalibratedLatticeEnsemble, self).__init__(**kwargs)
      return
    if model_config is None:
      raise ValueError('Must provide a model_config.')
    # Check that proper config has been given.
    if not isinstance(model_config, configs.CalibratedLatticeEnsembleConfig):
      raise ValueError('Invalid config type: {}'.format(type(model_config)))
    # Verify that the config is fully specified.
    premade_lib.verify_config(model_config)
    # Get feature configs and construct model.
    input_layer = premade_lib.build_input_layer(
        feature_configs=model_config.feature_configs, dtype=dtype)

    submodels_inputs = premade_lib.build_calibration_layers(
        calibration_input_layer=input_layer,
        feature_configs=model_config.feature_configs,
        model_config=model_config,
        layer_output_range=premade_lib.LayerOutputRange.INPUT_TO_LATTICE,
        submodels=model_config.lattices,
        separate_calibrators=model_config.separate_calibrators,
        dtype=dtype)

    lattice_outputs = []
    for submodel_index, (lattice_feature_names, lattice_input) in enumerate(
        zip(model_config.lattices, submodels_inputs)):
      lattice_feature_configs = [
          model_config.feature_config_by_name(feature_name)
          for feature_name in lattice_feature_names
      ]

      lattice_layer_output_range = (
          premade_lib.LayerOutputRange.INPUT_TO_FINAL_CALIBRATION
          if model_config.output_calibration else
          premade_lib.LayerOutputRange.MODEL_OUTPUT)
      lattice_outputs.append(
          premade_lib.build_lattice_layer(
              lattice_input=lattice_input,
              feature_configs=lattice_feature_configs,
              model_config=model_config,
              layer_output_range=lattice_layer_output_range,
              submodel_index=submodel_index,
              is_inside_ensemble=True,
              dtype=dtype))

    if len(lattice_outputs) > 1:
      averaged_lattice_output = tf.keras.layers.Average()(lattice_outputs)
    else:
      averaged_lattice_output = lattice_outputs[0]
    if model_config.output_calibration:
      model_output = premade_lib.build_output_calibration_layer(
          output_calibration_input=averaged_lattice_output,
          model_config=model_config,
          dtype=dtype)
    else:
      model_output = averaged_lattice_output

    # Define inputs and initialize model.
    inputs = [
        input_layer[feature_config.name]
        for feature_config in model_config.feature_configs
    ]
    kwargs['inputs'] = inputs
    kwargs['outputs'] = model_output
    super(CalibratedLatticeEnsemble, self).__init__(**kwargs)

  def get_config(self):
    """Returns a configuration dictionary."""
    config = super(CalibratedLatticeEnsemble, self).get_config()
    config['model_config'] = tf.keras.utils.serialize_keras_object(
        self.model_config)
    return config

  @classmethod
  def from_config(cls, config, custom_objects=None):
    custom_objects = _extend_custom_objects(custom_objects)
    model = super(CalibratedLatticeEnsemble, cls).from_config(
        config, custom_objects=custom_objects)
    try:
      model_config = tf.keras.utils.deserialize_keras_object(
          config.get('model_config'), custom_objects=custom_objects)
      premade_lib.verify_config(model_config)
      model.model_config = model_config
    except ValueError:
      logging.warning(
          'Could not load model_config. Constructing model without it: %s',
          str(config.get('model_config')))
    return model


class CalibratedLattice(tf.keras.Model):
  """Premade model for Tensorflow calibrated lattice models.

  Creates a `tf.keras.Model` for the model architecture specified by the
  `model_config`, which should be a `tfl.configs.CalibratedLatticeConfig`. No
  fields in the model config will be automatically filled in, so the config
  must be fully specified. Note that the inputs to the model should match the
  order in which they are defined in the feature configs.

  Example:

  ```python
  model_config = tfl.configs.CalibratedLatticeConfig(...)
  calibrated_lattice_model = tfl.premade.CalibratedLattice(
      model_config=model_config)
  calibrated_lattice_model.compile(...)
  calibrated_lattice_model.fit(...)
  ```

  Attributes:
    model_config: Model configuration object describing model architecture.
      Should be a `tfl.configs.CalibratedLatticeConfig` instance.
  """

  def __init__(self, model_config=None, dtype=tf.float32, **kwargs):
    """Initializes a `CalibratedLattice` instance.

    Args:
      model_config: Model configuration object describing model architecutre.
        Should be one of the model configs in `tfl.configs`.
      dtype: dtype of layers used in the model.
      **kwargs: Any additional `tf.keras.Model` arguments.
    """
    # Set our model_config
    self.model_config = model_config
    # Check if we are constructing with already provided inputs/outputs, e.g.
    # when we are loading a model.
    if 'inputs' in kwargs and 'outputs' in kwargs:
      super(CalibratedLattice, self).__init__(**kwargs)
      return
    if model_config is None:
      raise ValueError('Must provide a model_config.')
    # Check that proper config has been given.
    if not isinstance(model_config, configs.CalibratedLatticeConfig):
      raise ValueError('Invalid config type: {}'.format(type(model_config)))
    # Verify that the config is fully specified.
    premade_lib.verify_config(model_config)
    # Get feature configs and construct model.
    input_layer = premade_lib.build_input_layer(
        feature_configs=model_config.feature_configs, dtype=dtype)
    submodels_inputs = premade_lib.build_calibration_layers(
        calibration_input_layer=input_layer,
        feature_configs=model_config.feature_configs,
        model_config=model_config,
        layer_output_range=premade_lib.LayerOutputRange.INPUT_TO_LATTICE,
        submodels=[[
            feature_config.name
            for feature_config in model_config.feature_configs
        ]],
        separate_calibrators=False,
        dtype=dtype)

    lattice_layer_output_range = (
        premade_lib.LayerOutputRange.INPUT_TO_FINAL_CALIBRATION
        if model_config.output_calibration else
        premade_lib.LayerOutputRange.MODEL_OUTPUT)
    lattice_output = premade_lib.build_lattice_layer(
        lattice_input=submodels_inputs[0],
        feature_configs=model_config.feature_configs,
        model_config=model_config,
        layer_output_range=lattice_layer_output_range,
        submodel_index=0,
        is_inside_ensemble=False,
        dtype=dtype)

    if model_config.output_calibration:
      model_output = premade_lib.build_output_calibration_layer(
          output_calibration_input=lattice_output,
          model_config=model_config,
          dtype=dtype)
    else:
      model_output = lattice_output

    # Define inputs and initialize model.
    inputs = [
        input_layer[feature_config.name]
        for feature_config in model_config.feature_configs
    ]
    kwargs['inputs'] = inputs
    kwargs['outputs'] = model_output
    super(CalibratedLattice, self).__init__(**kwargs)

  def get_config(self):
    """Returns a configuration dictionary."""
    config = super(CalibratedLattice, self).get_config()
    config['model_config'] = tf.keras.utils.serialize_keras_object(
        self.model_config)
    return config

  @classmethod
  def from_config(cls, config, custom_objects=None):
    custom_objects = _extend_custom_objects(custom_objects)
    model = super(CalibratedLattice, cls).from_config(
        config, custom_objects=custom_objects)
    try:
      model_config = tf.keras.utils.deserialize_keras_object(
          config.get('model_config'), custom_objects=custom_objects)
      premade_lib.verify_config(model_config)
      model.model_config = model_config
    except ValueError:
      logging.warning(
          'Could not load model_config. Constructing model without it: %s',
          str(config.get('model_config')))
    return model


class CalibratedLinear(tf.keras.Model):
  """Premade model for Tensorflow calibrated linear models.

  Creates a `tf.keras.Model` for the model architecture specified by the
  `model_config`, which should be a `tfl.configs.CalibratedLinearConfig`. No
  fields in the model config will be automatically filled in, so the config
  must be fully specified. Note that the inputs to the model should match the
  order in which they are defined in the feature configs.

  Example:

  ```python
  model_config = tfl.configs.CalibratedLinearConfig(...)
  calibrated_linear_model = tfl.premade.CalibratedLinear(
      model_config=model_config)
  calibrated_linear_model.compile(...)
  calibrated_linear_model.fit(...)
  ```

  Attributes:
    model_config: Model configuration object describing model architecture.
      Should be a `tfl.configs.CalibratedLinearConfig` instance.
  """

  def __init__(self, model_config=None, dtype=tf.float32, **kwargs):
    """Initializes a `CalibratedLinear` instance.

    Args:
      model_config: Model configuration object describing model architecutre.
        Should be one of the model configs in `tfl.configs`.
      dtype: dtype of layers used in the model.
      **kwargs: Any additional `tf.keras.Model` arguments.
    """
    # Set our model_config
    self.model_config = model_config
    # Check if we are constructing with already provided inputs/outputs, e.g.
    # when we are loading a model.
    if 'inputs' in kwargs and 'outputs' in kwargs:
      super(CalibratedLinear, self).__init__(**kwargs)
      return
    if model_config is None:
      raise ValueError('Must provide a model_config.')
    # Check that proper config has been given.
    if not isinstance(model_config, configs.CalibratedLinearConfig):
      raise ValueError('Invalid config type: {}'.format(type(model_config)))
    # Verify that the config is fully specified.
    premade_lib.verify_config(model_config)
    # Get feature configs and construct model.
    input_layer = premade_lib.build_input_layer(
        feature_configs=model_config.feature_configs, dtype=dtype)

    calibration_layer_output_range = (
        premade_lib.LayerOutputRange.INPUT_TO_FINAL_CALIBRATION
        if model_config.output_calibration else
        premade_lib.LayerOutputRange.MODEL_OUTPUT)
    submodels_inputs = premade_lib.build_calibration_layers(
        calibration_input_layer=input_layer,
        feature_configs=model_config.feature_configs,
        model_config=model_config,
        layer_output_range=calibration_layer_output_range,
        submodels=[[
            feature_config.name
            for feature_config in model_config.feature_configs
        ]],
        separate_calibrators=False,
        dtype=dtype)

    weighted_average = (
        model_config.output_min is not None or
        model_config.output_max is not None or
        model_config.output_calibration)
    linear_output = premade_lib.build_linear_layer(
        linear_input=submodels_inputs[0],
        feature_configs=model_config.feature_configs,
        model_config=model_config,
        weighted_average=weighted_average,
        submodel_index=0,
        dtype=dtype)

    if model_config.output_calibration:
      model_output = premade_lib.build_output_calibration_layer(
          output_calibration_input=linear_output,
          model_config=model_config,
          dtype=dtype)
    else:
      model_output = linear_output

    # Define inputs and initialize model.
    inputs = [
        input_layer[feature_config.name]
        for feature_config in model_config.feature_configs
    ]
    kwargs['inputs'] = inputs
    kwargs['outputs'] = model_output
    super(CalibratedLinear, self).__init__(**kwargs)

  def get_config(self):
    """Returns a configuration dictionary."""
    config = super(CalibratedLinear, self).get_config()
    config['model_config'] = tf.keras.utils.serialize_keras_object(
        self.model_config)
    return config

  @classmethod
  def from_config(cls, config, custom_objects=None):
    custom_objects = _extend_custom_objects(custom_objects)
    model = super(CalibratedLinear, cls).from_config(
        config, custom_objects=custom_objects)
    try:
      model_config = tf.keras.utils.deserialize_keras_object(
          config.get('model_config'), custom_objects=custom_objects)
      premade_lib.verify_config(model_config)
      model.model_config = model_config
    except ValueError:
      logging.warning(
          'Could not load model_config. Constructing model without it: %s',
          str(config.get('model_config')))
    return model


# TODO: add support for tf.map_fn and inputs of shape (B, ?, input_dim)
# as well as non-ragged inputs using padding/mask.
class AggregateFunction(tf.keras.Model):
  """Premade model for Tensorflow aggregate function learning models.

  Creates a `tf.keras.Model` for the model architecture specified by the
  `model_config`, which should be a
  `tfl.configs.AggregateFunctionConfig`. No
  fields in the model config will be automatically filled in, so the config
  must be fully specified. Note that the inputs to the model should match the
  order in which they are defined in the feature configs. Features will be
  considered ragged, so inputs to this model must be `tf.ragged` instances.

  Example:

  ```python
  model_config = tfl.configs.AggregateFunctionConfig(...)
  agg_model = tfl.premade.AggregateFunction(
      model_config=model_config)
  agg_model.compile(...)
  agg_model.fit(...)
  ```
  """

  def __init__(self, model_config=None, dtype=tf.float32, **kwargs):
    """Initializes an `AggregateFunction` instance.

    Args:
      model_config: Model configuration object describing model architecutre.
        Should be a `tfl.configs.AggregateFunctionConfig` instance.
      dtype: dtype of layers used in the model.
      **kwargs: Any additional `tf.keras.Model` arguments.
    """
    # Set our model_config
    self.model_config = model_config
    # Check if we are constructing with already provided inputs/outputs, e.g.
    # when we are loading a model.
    if 'inputs' in kwargs and 'outputs' in kwargs:
      super(AggregateFunction, self).__init__(**kwargs)
      return
    if model_config is None:
      raise ValueError('Must provide a model_config.')
    # Check that proper config has been given.
    if not isinstance(model_config, configs.AggregateFunctionConfig):
      raise ValueError('Invalid config type: {}'.format(type(model_config)))
    # Verify that the config is fully specified.
    premade_lib.verify_config(model_config)
    # Get feature configs and construct model.
    input_layer = premade_lib.build_input_layer(
        feature_configs=model_config.feature_configs,
        dtype=dtype,
        ragged=True)

    # We need to construct middle_dimension calibrated_lattices for the
    # aggregation layer. Note that we cannot do this in premade_lib because
    # importing premade in premade_lib would cause a dependency cycle. Also
    # note that we only need to set the output initialization to the min and
    # max since we are not using output calibration at this step of the
    # aggregation.
    calibrated_lattice_config = configs.CalibratedLatticeConfig(
        feature_configs=model_config.feature_configs,
        regularizer_configs=model_config.regularizer_configs,
        output_min=-1.0,
        output_max=1.0,
        output_initialization=[-1.0, 1.0])
    calibrated_lattice_models = [
        CalibratedLattice(calibrated_lattice_config)
        for _ in range(model_config.middle_dimension)
    ]
    aggregation_layer_output_range = (
        premade_lib.LayerOutputRange.INPUT_TO_FINAL_CALIBRATION
        if model_config.output_calibration else
        premade_lib.LayerOutputRange.MODEL_OUTPUT)
    aggregation_output = premade_lib.build_aggregation_layer(
        aggregation_input_layer=input_layer,
        model_config=model_config,
        calibrated_lattice_models=calibrated_lattice_models,
        layer_output_range=aggregation_layer_output_range,
        submodel_index=0,
        dtype=dtype)

    if model_config.output_calibration:
      model_output = premade_lib.build_output_calibration_layer(
          output_calibration_input=aggregation_output,
          model_config=model_config,
          dtype=dtype)
    else:
      model_output = aggregation_output

    # Define inputs and initialize model.
    inputs = [
        input_layer[feature_config.name]
        for feature_config in model_config.feature_configs
    ]
    super(AggregateFunction, self).__init__(
        inputs=inputs, outputs=model_output)

  def get_config(self):
    """Returns a configuration dictionary."""
    config = super(AggregateFunction, self).get_config()
    config['model_config'] = tf.keras.utils.serialize_keras_object(
        self.model_config)
    return config

  @classmethod
  def from_config(cls, config, custom_objects=None):
    custom_objects = _extend_custom_objects(custom_objects)
    model = super(AggregateFunction, cls).from_config(
        config, custom_objects=custom_objects)
    try:
      model_config = tf.keras.utils.deserialize_keras_object(
          config.get('model_config'), custom_objects=custom_objects)
      premade_lib.verify_config(model_config)
      model.model_config = model_config
    except ValueError:
      logging.warning(
          'Could not load model_config. Constructing model without it: %s',
          str(config.get('model_config')))
    return model


def _extend_custom_objects(custom_objects):
  """Extends the given custom_objects mapping with TFL objects."""
  tfl_custom_objects = {
      'AggregateFunction':
          AggregateFunction,
      'CalibratedLatticeEnsemble':
          CalibratedLatticeEnsemble,
      'CalibratedLattice':
          CalibratedLattice,
      'CalibratedLinear':
          CalibratedLinear,
      'CategoricalCalibration':
          categorical_calibration_layer.CategoricalCalibration,
      'CategoricalCalibrationConstraints':
          categorical_calibration_layer.CategoricalCalibrationConstraints,
      'FeatureConfig':
          configs.FeatureConfig,
      'RegularizerConfig':
          configs.RegularizerConfig,
      'TrustConfig':
          configs.TrustConfig,
      'DominanceConfig':
          configs.DominanceConfig,
      'CalibratedLatticeEnsembleConfig':
          configs.CalibratedLatticeEnsembleConfig,
      'CalibratedLatticeConfig':
          configs.CalibratedLatticeConfig,
      'CalibratedLinearConfig':
          configs.CalibratedLinearConfig,
      'AggregateFunctionConfig':
          configs.AggregateFunctionConfig,
      'Aggregation':
          aggregation_layer.Aggregation,
      'Lattice':
          lattice_layer.Lattice,
      'LatticeConstraints':
          lattice_layer.LatticeConstraints,
      'Linear':
          linear_layer.Linear,
      'LinearConstraints':
          linear_layer.LinearConstraints,
      'ParallelCombination':
          parallel_combination_layer.ParallelCombination,
      'PWLCalibration':
          pwl_calibration_layer.PWLCalibration,
      'PWLCalibrationConstraints':
          pwl_calibration_layer.PWLCalibrationConstraints,
      'NaiveBoundsConstraints':
          pwl_calibration_layer.NaiveBoundsConstraints,
  }
  if custom_objects is not None:
    tfl_custom_objects.update(custom_objects)
  return tfl_custom_objects
