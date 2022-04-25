import json
import logging
import os

import pytest
from dotenv import dotenv_values
from filelock import FileLock

config = dotenv_values(".env")

logging.getLogger("elasticsearch").setLevel("WARN")
logging.getLogger("botocore").setLevel("WARN")


def pytest_configure(config: pytest.Config):
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is not None:
        # log_file = config.getini("worker_log_file")
        logging.basicConfig(
            format=config.getini("log_file_format"),
            filename=f"tests_{worker_id}.log",
            level=config.getini("log_file_level"),
        )


@pytest.fixture(scope="session", autouse=True)
def setup_session(tmp_path_factory, worker_id):
    logging.info("*****SETUP SESSION*****")

    if worker_id == "master":
        logging.debug("Single worker mode detected.")
        logging.info("Executing session setup.")
        # not executing in with multiple workers, just produce the data and let
        # pytest's fixture caching do its job
        clear_pcm_test_state()
        return

    logging.debug("Multiple worker mode detected.")

        # get the temp directory shared by all workers
    root_tmp_dir = tmp_path_factory.getbasetemp().parent

    fn = root_tmp_dir / "data.json"
    with FileLock(str(fn) + ".lock"):
        if fn.is_file():
            logging.info("Session setup already executed by a different worker.")
            return
        else:
            logging.info("Executing session setup.")
            clear_pcm_test_state()
            fn.write_text(json.dumps({}))
    return


def clear_pcm_test_state():
    from int_test_util import \
        es_index_delete, \
        delete_output_files

    es_index_delete("grq_1_l2_hls_l30")
    es_index_delete("grq_1_l2_hls_s30")
    es_index_delete("grq_1_opera_state_config")
    es_index_delete("grq_1_l3_dswx_hls")
    delete_output_files(bucket=config["RS_BUCKET"], prefix="products/")
