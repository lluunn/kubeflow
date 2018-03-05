#!/usr/bin/env python

# Copyright 2018 The Kubeflow Authors All rights reserved.
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

from __future__ import print_function

from itertools import repeat
import base64
import logging

from grpc.beta import implementations
from tensorflow_serving.apis import predict_pb2
from tensorflow_serving.apis import prediction_service_pb2
from tensorflow_serving.apis import get_model_metadata_pb2
from tornado import gen
from tornado.ioloop import IOLoop
from tornado.options import define, options, parse_command_line
import tensorflow as tf
from tensorflow.python.saved_model import signature_constants
import tornado.web


define("port", default=8888, help="run on the given port", type=int)
define("rpc_timeout", default=1.0, help="seconds for time out rpc request", type=float)
define("rpc_port", default=9000, help="tf serving on the given port", type=int)
define("rpc_address", default='localhost', help="tf serving on the given address", type=str)
define("instances_key", default='instances', help="requested instances json object key")
define("debug", default=False, help="run in debug mode")
B64_KEY = 'b64'
WELCOME = "Hello World"
MODEL_SERVER_METADATA_TIMEOUT_SEC = 20

#### START code took from https://github.com/grpc/grpc/wiki/Integration-with-tornado-(python)

def _fwrap(f, gf):
  try:
    f.set_result(gf.result())
  except Exception as e:
    f.set_exception(e)


def fwrap(gf, ioloop=None):
  '''
  Wraps a GRPC result in a future that can be yielded by tornado
      
    Usage::
      
      @coroutine
      def my_fn(param):
        result = yield fwrap(stub.function_name.future(param, timeout))
  '''
  f = gen.Future()

  if ioloop is None:
    ioloop = IOLoop.current()

  gf.add_done_callback(lambda _: ioloop.add_callback(_fwrap, f, gf))
  return f

#### END code took from https://github.com/grpc/grpc/wiki/Integration-with-tornado-(python)

def decode_b64_if_needed(data):
  if isinstance(data, list):
    return [decode_b64_if_needed(val) for val in data]
  elif isinstance(data, dict):
    if data.viewkeys() == {"b64"}:
      return base64.b64decode(data["b64"])
    else:
      return {k: decode_b64_if_needed(v) for k, v in data.iteritems()}
  else:
    return data

def get_signature_map(model_server_stub, model_name):
  """ Gets tensorflow signature map from the model server stub.

  Args:
    model_server_stub: The grpc stub to call GetModelMetadata.
    model_name: The model name.

  Returns:
    The signature map of the model.
  """
  request = get_model_metadata_pb2.GetModelMetadataRequest()
  request.model_spec.name = model_name
  request.metadata_field.append("signature_def")
  try:
    response = model_server_stub.GetModelMetadata(request, MODEL_SERVER_METADATA_TIMEOUT_SEC)
  except grpc.RpcError as rpc_error:
    logging.exception("GetModelMetadata call to model server failed with code "
                      "%s and message %s", rpc_error.code(),
                      rpc_error.details())
    return None

  signature_def_map_proto = get_model_metadata_pb2.SignatureDefMap()
  response.metadata["signature_def"].Unpack(signature_def_map_proto)
  signature_def_map = signature_def_map_proto.signature_def
  if not signature_def_map:
    logging.error("Graph has no signatures.")

  # Delete incomplete signatures without input dtypes.
  invalid_signatures = []
  for signature_name in signature_def_map:
    for tensor in signature_def_map[signature_name].inputs.itervalues():
      if not tensor.dtype:
        logging.warn("Signature %s has incomplete dtypes, removing from "
                     "usable signatures", signature_name)
        invalid_signatures.append(signature_name)
        break
  for signature_name in invalid_signatures:
    del signature_def_map[signature_name]

  return signature_def_map

def get_signature(signature_map, signature_name=None):
  """Gets tensorflow signature for the given signature_name.

  Args:
    signature_name: string The signature name to use to choose the signature
                    from the signature map.

  Returns:
    a pair of signature_name and signature. The first element is the
    signature name in string that is actually used. The second one is the
    signature.

  Raises:
    KeyError: when the signature is not found with the given signature
    name or when there are more than one signatures in the signature map.
  """
  # The way to find signature is:
  # 1) if signature_name is specified, try to find it in the signature_map. If
  # not found, raise an exception.
  # 2) if signature_name is not specified, check if signature_map only
  # contains one entry. If so, return the only signature.
  # 3) Otherwise, use the default signature_name and do 1).
  if not signature_name and len(signature_map) == 1:
    return signature_map.keys()[0], signature_map.values()[0]

  key = (signature_name or
         signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY)
  if key in signature_map:
    return key, signature_map[key]
  else:
    raise KeyError("No signature found for signature key %s." % signature_name)

class PredictHandler(tornado.web.RequestHandler):

  @gen.coroutine
  def post(self, model_name, version_name=None):
    if not self.settings['signature_map'].get(model_name):
      self.settings['signature_map'][model_name] = get_signature_map(self.settings['stub'], model_name)

    request_key = self.settings['request_key']
    request_data = tornado.escape.json_decode(self.request.body)
    instances = request_data.get(request_key)
    if not instances:
      self.send_error('Request json object have to use the key: %s' % request_key)
    if len(instances) < 1 or not isinstance(instances, (list, tuple)):
      self.send_error('Request instances object have to use be a list')
    instances = decode_b64_if_needed(instances)

    signature_name = request_data.get("signature_name")
    signature_name_used, signature = get_signature(self.settings['signature_map'][model_name],
                                                   signature_name)
    input_columns = signature.inputs.keys()

    request = predict_pb2.PredictRequest()
    request.model_spec.name = model_name
    request.model_spec.signature_name = signature_name_used

    if version_name is not None:
      request.model_spec.version = version_name
    
    inputs_type_map = signature.inputs
    for input_column in input_columns:
      values = [instance[input_column] for instance in instances]
      request.inputs[input_column].CopyFrom(tf.make_tensor_proto(values, inputs_type_map[input_column].dtype))

    stub = self.settings['stub']
    result = yield fwrap(stub.Predict.future(request, self.settings['rpc_timeout']))
    output_keys = result.outputs.keys()
    predictions = zip(*[tf.make_ndarray(result.outputs[output_key]).tolist() for output_key in output_keys])
    predictions = [dict(zip(*t)) for t in zip(repeat(output_keys), predictions)]
    self.write(dict(predictions=predictions))


class IndexHanlder(tornado.web.RequestHandler):
  def get(self):
    self.write('Hello World')


def get_application(**settings):
  return tornado.web.Application(
      [
      (r"/model/(.*):predict", PredictHandler),
      (r"/model/(.*)/version/(.*):predict", PredictHandler),
      (r"/", IndexHanlder),
      ],
      xsrf_cookies=False,
      debug=options.debug,
      rpc_timeout = options.rpc_timeout,
      request_key = options.instances_key,
      **settings)

def main():
  parse_command_line()

  channel = implementations.insecure_channel(options.rpc_address, options.rpc_port)
  stub = prediction_service_pb2.beta_create_PredictionService_stub(channel)
  extra_settings = dict(
      stub = stub,
      signature_map = {},
  )
  app = get_application(**extra_settings)
  app.listen(options.port)
  logging.info('running at http://localhost:%s'%options.port)
  tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
  main()
