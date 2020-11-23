from google.cloud.logging import Client
from google.cloud.logging import _helpers
from google.cloud.logging.handlers import CloudLoggingHandler
from google.cloud.logging.handlers.transports.background_thread import BackgroundThreadTransport
# from google.cloud.logging.handlers.transports.sync import SyncTransport

from google.cloud.logging.resource import Resource

from pythonjsonlogger import jsonlogger
import structlog

from functools import lru_cache as only_run_once
import json
import logging
import requests
import os

def flog(msg):
  with open('/tmp/hotflights.flog', 'a') as daflog:
    daflog.write(str(msg) + "\n")

class StructlogTransport(BackgroundThreadTransport):
  def send(self, record, message, resource=None, labels=None, trace=None, span_id=None):
    info = queue_entry_from_structlog_json(record, message, resource=None, labels=None, trace=None, span_id=None)
    self.logger.log_struct(
        info,
        severity=_helpers._normalize_severity(record.levelno),
        resource=resource,
        labels=labels,
        trace=trace,
        span_id=span_id,
    )

def queue_entry_from_structlog_json(record, message, resource=None, labels=None, trace=None, span_id=None):  
  try:
    info = json.loads(message)
  except json.decoder.JSONDecodeError:
    info = { "message": message }
  finally:
    info["python_logger"] = record.name
    if not info.get("message"):
      # move Structlog's log['event'] field to Google Stackdrivers jsonPayload.message key
      STRUCTLOG_MESSAGE_KEY = "event"
      info["message"] = info.get(STRUCTLOG_MESSAGE_KEY)
      info.pop(STRUCTLOG_MESSAGE_KEY, None)
  return info

def configure_structlog():
  structlog.configure(
      processors=[
          structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
      ],
      logger_factory=structlog.stdlib.LoggerFactory(),
  )

def get_handler(logName):
  kwargs = {}
  try:
    kwargs['resource'] = get_log_resource_for_gce_instance()
  except:
    # Probably not on GCE ;-)
    pass

  # TODO: When we launched celery workers using prefork (multiprocessing: separate process per worker)
  # we found that from google.cloud.logging.handlers.transports.background_thread.BackgroundThreadTransport
  # stopped transmitting logs to GCP. We're not sure why, but as a workaround we switched to using
  # a SyncTransport sub-class.
  handler = CloudLoggingHandler(Client(), logName, transport=StructlogTransport, **kwargs)
  handler.setFormatter(jsonlogger.JsonFormatter())
  return handler

def get_default_logging_namespace():
  try:
    import __main__
    return __main__.__loader__.name.split('.')[0]
  except:
    pass

def get_log_resource_for_gce_instance():
  # GCE logs not touched by us by default show up under "Cloud Logs" link from the instance
  # To match this, we need to set the resource field correctly in our logging to match these:

  # EXAMPLE FROM A DEFAULT VM INSTANCE LOG MESSAGE:
  #
  # resource: {
  #   type: "gce_instance"
  #   labels: {
  #     project_id: "ceres-imaging-science"
  #     instance_id: "6201251793328237718"
  #     zone: "us-west1-a"
  #   }
  # }

  # EXAMPLE QUERY OUTPUT BY STACKDRIVER FOR A VM INSTANCE:
  #
  # resource.type="gce_instance"
  # resource.labels.instance_id="6201251793328237718"


  # To do this, we're going to use the GCE computeMetadata endpoint
  # which will give us this info (or fail if we're not on GCE)
  # 
  # For a list of all properties we could query on computeMetadata,
  # see: https://cloud.google.com/compute/docs/storing-retrieving-metadata

  metadata_server = "http://metadata/computeMetadata/v1/"
  metadata_flavor = {'Metadata-Flavor' : 'Google'}
  
  get_compute_metadata = lambda propPath: requests.get(metadata_server + propPath, headers=metadata_flavor).text

  return Resource(type='gce_instance', labels={
    'instance_id': get_compute_metadata('instance/id'),
    'project_id': get_compute_metadata('project/project-id'),
    'zone': get_compute_metadata('instance/zone').split('/')[-1],
  })

@only_run_once(maxsize=32)
def setup_google_logger(log_name=get_default_logging_namespace()):
  flog("setup_google_logger()")
  configure_structlog()
  
  google_handler = None
  def _setup_google_logger():
    nonlocal google_handler
    
    flog("_setup_google_logger()")
    root_logger = logging.getLogger()

    if google_handler:
      flog("removing existing handler")
      root_logger.removeHandler(google_handler)
    else:
      flog("no existing handler")

    # Add google_structlog handler to the root logger
    google_handler = get_handler(log_name)
    root_logger.addHandler(google_handler)
    flog("exiting _setup_google_logger()")

  _setup_google_logger()

  # If using celery or multiprocessing, we need to restart the 
  # google logging handler thread after a fork
  os.register_at_fork(after_in_child=_setup_google_logger)
  flog("exiting setup_google_logger()")