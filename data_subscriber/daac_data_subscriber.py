#!/usr/bin/env python3

# Forked from github.com:podaac/data-subscriber.git


import argparse
import asyncio
import itertools
import json
import logging
import netrc
import os
import re
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from http.cookiejar import CookieJar
from multiprocessing.pool import ThreadPool
from pathlib import Path
from typing import Any, Iterable
from urllib import request
from urllib.parse import urlparse

import boto3
import requests
from hysds_commons.job_utils import submit_mozart_job
from more_itertools import map_reduce, chunked
from smart_open import open

from data_subscriber.es_connection import get_data_subscriber_connection


class SessionWithHeaderRedirection(requests.Session):
    """
    Borrowed from https://wiki.earthdata.nasa.gov/display/EL/How+To+Access+Data+With+Python
    """

    def __init__(self, username, password, auth_host):
        super().__init__()
        self.auth = (username, password)
        self.auth_host = auth_host

    # Overrides from the library to keep headers when redirected to or from
    # the NASA auth host.
    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        url = prepared_request.url

        if 'Authorization' in headers:
            original_parsed = requests.utils.urlparse(response.request.url)
            redirect_parsed = requests.utils.urlparse(url)
            if (original_parsed.hostname != redirect_parsed.hostname) and \
                    redirect_parsed.hostname != self.auth_host and \
                    original_parsed.hostname != self.auth_host:
                del headers['Authorization']


async def run(argv: list[str]):
    parser = create_parser()
    args = parser.parse_args(argv[1:])
    try:
        validate(args)
    except ValueError as v:
        logging.error(v)
        exit()

    IP_ADDR = "127.0.0.1"
    EDL = "urs.earthdata.nasa.gov"
    CMR = "cmr.earthdata.nasa.gov"
    TOKEN_URL = f"https://{CMR}/legacy-services/rest/tokens"
    NETLOC = urlparse("https://urs.earthdata.nasa.gov").netloc
    ES_CONN = get_data_subscriber_connection(logging.getLogger(__name__))
    LOGLEVEL = 'DEBUG' if args.verbose else 'INFO'
    logging.basicConfig(level=LOGLEVEL)
    logging.info("Log level set to " + LOGLEVEL)

    logging.info(f"{argv=}")

    with open("_job.json", "r+") as job:
        logging.info("job_path: {}".format(job))
        local_job_json = json.load(job)
        logging.info(f"{local_job_json=!s}")
    job_id = local_job_json["job_info"]["job_payload"]["payload_task_id"]
    logging.info(f"{job_id=}")

    username, password = setup_earthdata_login_auth(EDL)

    with token_ctx(TOKEN_URL, IP_ADDR, EDL) as token:
        if args.index_mode == "query":
            results = await run_query(args, token, ES_CONN, CMR, job_id)
        elif args.index_mode == "query-and-something":
            results = await run_query(args, token, ES_CONN, CMR, job_id)
            # TODO chridjrd: implement meee
        elif args.index_mode == "download":
            results = run_download(args, token, ES_CONN, NETLOC, username, password, job_id)
        else:
            raise Exception(f"Unsupported operation. {args.index_mode=}")  # TODO chrisjrd: implement
    logging.info("END")
    return results


async def run_query(args, token, ES_CONN, CMR, job_id):
    download_urls: list[str] = query_cmr(args, token, CMR)
    if not download_urls:
        return

    update_es_index(ES_CONN, download_urls, job_id)

    tile_id_to_urls_map: dict[str, set[str]] = map_reduce(
        iterable=download_urls,
        keyfunc=url_to_tile_id,
        valuefunc=lambda url: url,
        reducefunc=set
    )

    if args.smoke_run:
        logging.info(f"{args.smoke_run=}. Restricting to a single tile.")
        tile_id_to_urls_map = dict(itertools.islice(tile_id_to_urls_map.items(), 1))

    logging.info(f"{tile_id_to_urls_map=}")
    job_submission_tasks = []
    loop = asyncio.get_event_loop()
    # TODO chrisjrd: finalize chunk size
    #  chunk_size=1 means 1 tile per job
    # chunk_size>1 means multiple (N) tiles per job
    chunk_size = 2
    logging.info(f"{chunk_size=}")
    for tile_chunk in chunked(tile_id_to_urls_map.items(), n=chunk_size):
        chunk_id = str(uuid.uuid4())
        logging.info(f"{chunk_id=}")

        chunk_tile_ids = []
        chunk_urls = []
        for tile_id, urls in tile_chunk:
            chunk_tile_ids.append(tile_id)
            chunk_urls.extend(urls)

        logging.info(f"{chunk_tile_ids=}")
        logging.info(f"{chunk_urls=}")

        job_submission_tasks.append(
            loop.run_in_executor(
                executor=None,
                func=partial(
                    submit_download_job,
                    params=[
                        {"name": "tile_ids", "value": " ".join(chunk_tile_ids), "from": "value"},
                        {"name": "isl_bucket_name", "value": args.s3_bucket, "from": "value"},
                        {"name": "start_time", "value": args.startDate, "from": "value"},
                        {"name": "end_time", "value": args.endDate, "from": "value"},

                        # TODO chrisjrd: remove this if possible
                        # NOTE: need to add dummy `isl_staging_area` param even though it is currently not used
                        {"name": "isl_staging_area", "value": "dummy", "from": "value"},

                        # TODO chrisjrd: implement support
                        # {"name": "smoke_run", "value": True, "from": "value"},
                        # {"name": "dry_run", "value": True, "from": "value"},

                    ]
                )
            )
        )

    results = await asyncio.gather(*job_submission_tasks, return_exceptions=True)
    logging.info(f"{len(results)=}")
    logging.info(f"{results=}")

    succeeded = [job_id for job_id in results if isinstance(job_id, str)]
    logging.info(f"{succeeded=}")
    failed = [e for e in results if isinstance(e, Exception)]
    logging.info(f"{failed=}")
    return {
        "success": succeeded,
        "fail": failed
    }


def run_download(args, token, ES_CONN, NETLOC, username, password, job_id):
    downloads: Iterable[dict] = ES_CONN.get_all_undownloaded()
    logging.info(f"{downloads=}")
    if not downloads:
        return

    if args.smoke_run:
        logging.info(f"{args.smoke_run=}. Restricting to a single file.")
        args.tile_ids = args.tile_ids[:1]

    downloads = list(filter(lambda d: to_tile_id(d) in args.tile_ids, downloads))

    download_urls = [to_url(download) for download in downloads]
    logging.info(f"{download_urls=}")
    session = SessionWithHeaderRedirection(username, password, NETLOC)

    # TODO chrisjrd: re-enable
    if args.transfer_protocol == "https":
        upload_url_list_from_https(session, ES_CONN, download_urls, args, token, job_id)
    else:
        upload_url_list_from_s3(session, ES_CONN, download_urls, args, job_id)


def submit_download_job(*, params: list[dict[str, str]]) -> str:
    return submit_mozart_job_minimal(
        hysdsio={
            "id": "test_id",
            "params": params,
            "job-specification": "job-data_subscriber_download:issue_85",  # TODO chrisjrd: TBD
        }
    )


def submit_mozart_job_minimal(*, hysdsio: dict) -> str:
    return submit_mozart_job(
        hysdsio=hysdsio,
        product={},
        rule={
            "rule_name": "trigger_data_subscriber_download_TBD",  # TODO chrisjrd: TBD
            "queue": "opera-job_worker-small",  # TODO chrisjrd: TBD
            "priority": "0",
            "kwargs": "{}",
            "enable_dedup": True
        },
        queue=None,
        job_name="job_TBD",  # TODO chrisjrd: TBD
        payload_hash=None,
        enable_dedup=None,
        soft_time_limit=None,
        time_limit=None,
        component=None
    )


def to_tile_id(dl_doc: dict[str, Any]):
    return url_to_tile_id(to_url(dl_doc))


def url_to_tile_id(url: str):
    input_filename = Path(url).name
    tile_id: str = re.findall(r"T\w{5}", input_filename)[0]
    return tile_id


def to_url(dl_dict: dict[str, Any]) -> str:
    if dl_dict.get("https_url"):
        return dl_dict["https_url"]
    elif dl_dict.get("s3_url"):
        return dl_dict["s3_url"]
    else:
        raise Exception(f"Couldn't find any URL in {dl_dict=}")


def create_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument("-c", "--collection-shortname", dest="collection", required=True,
                        help="The collection shortname for which you want to retrieve data.")
    parser.add_argument("-s", "--s3bucket", dest="s3_bucket", required=True,
                        help="The s3 bucket where data products will be downloaded.")
    parser.add_argument("-sd", "--start-date", dest="startDate", default=False,
                        help="The ISO date time after which data should be retrieved. For Example, --start-date 2021-01-14T00:00:00Z")
    parser.add_argument("-ed", "--end-date", dest="endDate", default=False,
                        help="The ISO date time before which data should be retrieved. For Example, --end-date 2021-01-14T00:00:00Z")
    parser.add_argument("-b", "--bounds", dest="bbox", default="-180,-90,180,90",
                        help="The bounding rectangle to filter result in. Format is W Longitude,S Latitude,E Longitude,N Latitude without spaces. Due to an issue with parsing arguments, to use this command, please use the -b=\"-180,-90,180,90\" syntax when calling from the command line. Default: \"-180,-90,180,90\".")
    parser.add_argument("-m", "--minutes", dest="minutes", type=int, default=60,
                        help="How far back in time, in minutes, should the script look for data. If running this script as a cron, this value should be equal to or greater than how often your cron runs (default: 60 minutes).")
    parser.add_argument("-e", "--extension_list", dest="extension_list", default="TIF",
                        help="The file extension mapping of products to download (band/mask). Defaults to all .tif files.")
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Verbose mode.")
    parser.add_argument("-p", "--provider", dest="provider", default='LPCLOUD',
                        help="Specify a provider for collection search. Default is LPCLOUD.")
    parser.add_argument("-i", "--index-mode", dest="index_mode", default="Disabled",
                        help="-i \"query\" will execute the query and update the ES index without downloading files. "
                             "-i \"download\" will download all files from the ES index marked as not yet downloaded.")
    parser.add_argument("-x", "--transfer-protocol", dest="transfer_protocol", default='s3',
                        help="The protocol used for retrieving data, HTTPS or default of S3")

    parser.add_argument("--tile-ids", nargs="*", dest="tile_ids")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--smoke-run", dest="smoke_run", action="store_true")

    return parser


def validate(args):
    validate_bounds(args.bbox)

    if args.startDate:
        validate_date(args.startDate, "start")

    if args.endDate:
        validate_date(args.endDate, "end")

    if args.minutes:
        validate_minutes(args.minutes)


def validate_bounds(bbox):
    bounds = bbox.split(',')
    value_error = ValueError(
        f"Error parsing bounds: {bbox}. Format is <W Longitude>,<S Latitude>,<E Longitude>,<N Latitude> without spaces ")

    if len(bounds) != 4:
        raise value_error

    for b in bounds:
        try:
            float(b)
        except ValueError:
            raise value_error


def validate_date(date, type='start'):
    try:
        datetime.strptime(date, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise ValueError(
            f"Error parsing {type} date: {date}. Format must be like 2021-01-14T00:00:00Z")


def validate_minutes(minutes):
    try:
        int(minutes)
    except ValueError:
        raise ValueError(f"Error parsing minutes: {minutes}. Number must be an integer.")


def setup_earthdata_login_auth(endpoint):
    # ## Authentication setup
    #
    # This function will allow Python scripts to log into any Earthdata Login
    # application programmatically.  To avoid being prompted for
    # credentials every time you run and also allow clients such as curl to log in,
    # you can add the following to a `.netrc` (`_netrc` on Windows) file in
    # your home directory:
    #
    # ```
    # machine urs.earthdata.nasa.gov
    #     login <your username>
    #     password <your password>
    # ```
    #
    # Make sure that this file is only readable by the current user
    # or you will receive an error stating
    # "netrc access too permissive."
    #
    # `$ chmod 0600 ~/.netrc`
    #
    # You'll need to authenticate using the netrc method when running from
    # command line with [`papermill`](https://papermill.readthedocs.io/en/latest/).
    # You can log in manually by executing the cell below when running in the
    # notebook client in your browser.*

    """
    Set up the request library so that it authenticates against the given
    Earthdata Login endpoint and is able to track cookies between requests.
    This looks in the .netrc file first and if no credentials are found,
    it prompts for them.

    Valid endpoints include:
        urs.earthdata.nasa.gov - Earthdata Login production
    """
    username = password = ""
    try:
        username, _, password = netrc.netrc().authenticators(endpoint)
    except FileNotFoundError as e:
        logging.error("There's no .netrc file")
        raise e
    except TypeError as e:
        logging.error("The endpoint isn't in the netrc file")
        raise e

    manager = request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, endpoint, username, password)
    auth = request.HTTPBasicAuthHandler(manager)

    jar = CookieJar()
    processor = request.HTTPCookieProcessor(jar)
    opener = request.build_opener(auth, processor)
    opener.addheaders = [('User-agent', 'daac-subscriber')]
    request.install_opener(opener)

    return username, password


def get_token(url: str, client_id: str, user_ip: str, endpoint: str) -> str:
    username, _, password = netrc.netrc().authenticators(endpoint)
    xml = f"<?xml version='1.0' encoding='utf-8'?><token><username>{username}</username><password>{password}</password><client_id>{client_id}</client_id><user_ip_address>{user_ip}</user_ip_address></token>"
    headers = {'Content-Type': 'application/xml', 'Accept': 'application/json'}
    resp = requests.post(url, headers=headers, data=xml)
    response_content = json.loads(resp.content)
    token = response_content['token']['id']

    return token


def query_cmr(args, token, CMR):
    PAGE_SIZE = 2000
    EXTENSION_LIST_MAP = {"L30": ["B02", "B03", "B04", "B05", "B06", "B07", "Fmask"],
                          "S30": ["B02", "B03", "B04", "B8A", "B11", "B12", "Fmask"],
                          "TIF": ["tif"]}
    time_range_is_defined = args.startDate or args.endDate

    data_within_last_timestamp = args.startDate if time_range_is_defined else (
            datetime.utcnow() - timedelta(minutes=args.minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = f"https://{CMR}/search/granules.umm_json"
    params = {
        'scroll': "false",
        'page_size': PAGE_SIZE,
        'sort_key': "-start_date",
        'provider': args.provider,
        'ShortName': args.collection,
        'updated_since': data_within_last_timestamp,
        'token': token,
        'bounding_box': args.bbox,
    }

    if time_range_is_defined:
        temporal_range = get_temporal_range(data_within_last_timestamp, args.endDate,
                                            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
        params['temporal'] = temporal_range
        logging.debug("Temporal Range: " + temporal_range)

    logging.info("Provider: " + args.provider)
    logging.info("Updated Since: " + data_within_last_timestamp)

    product_urls, search_after = request_search(url, params)

    while search_after:
        results, search_after = request_search(url, params, search_after=search_after)
        product_urls.extend(results)

    # filter list based on extension
    filtered_urls = [f
                     for f in product_urls
                     for extension in EXTENSION_LIST_MAP.get(args.extension_list.upper())
                     if extension in f]

    logging.info(f"Found {str(len(filtered_urls))} total files")

    return filtered_urls


def get_temporal_range(start, end, now):
    start = start if start is not False else None
    end = end if end is not False else None

    if start is not None and end is not None:
        return "{},{}".format(start, end)
    if start is not None and end is None:
        return "{},{}".format(start, now)
    if start is None and end is not None:
        return "1900-01-01T00:00:00Z,{}".format(end)

    raise ValueError("One of start-date or end-date must be specified.")


def request_search(url, params, search_after=None):
    response = requests.get(url, params=params, headers={'CMR-Search-After': search_after}) \
        if search_after else requests.get(url, params=params)
    results = response.json()
    items = results.get('items')
    next_search_after = response.headers.get('CMR-Search-After')

    if items and items[0].get('umm'):
        return [meta.get('URL') for item in items for meta in item.get('umm').get('RelatedUrls')], next_search_after
    else:
        return [], None


def convert_datetime(datetime_obj, strformat="%Y-%m-%dT%H:%M:%S.%fZ"):
    if isinstance(datetime_obj, datetime):
        return datetime_obj.strftime(strformat)
    return datetime.strptime(str(datetime_obj), strformat)


def update_es_index(ES_CONN, download_urls, job_id):
    for url in download_urls:
        ES_CONN.process_url(url, job_id)  # Implicitly adds new product to index


def upload_url_list_from_https(session, ES_CONN, downloads, args, token, job_id):
    num_successes = num_failures = num_skipped = 0
    filtered_downloads = [f for f in downloads if "https://" in f]

    for url in filtered_downloads:
        try:
            if ES_CONN.product_is_downloaded(url):
                logging.info(f"SKIPPING: {url}")
                num_skipped = num_skipped + 1
            else:
                if args.dry_run:
                    logging.info(f"{args.dry_run=}. Skipping downloads.")
                else:
                    result = https_transfer(url, args.s3_bucket, session, token)
                    if "failed_download" in result:
                        raise Exception(result["failed_download"])
                    else:
                        logging.debug(str(result))

                ES_CONN.mark_product_as_downloaded(url, job_id)
                logging.info(f"{str(datetime.now())} SUCCESS: {url}")
                num_successes = num_successes + 1
        except Exception as e:
            logging.error(f"{str(datetime.now())} FAILURE: {url}")
            num_failures = num_failures + 1
            logging.error(e)

    logging.info(f"Files downloaded: {str(num_successes)}")
    logging.info(f"Duplicate files skipped: {str(num_skipped)}")
    logging.info(f"Files failed to download: {str(num_failures)}")


def https_transfer(url, bucket_name, session, token, staging_area="", chunk_size=25600):
    file_name = os.path.basename(url)
    bucket = bucket_name[len("s3://"):] if bucket_name.startswith("s3://") else bucket_name

    key = os.path.join(staging_area, file_name)
    upload_start_time = datetime.utcnow()
    headers = {"Echo-Token": token}

    try:
        with session.get(url, headers=headers, stream=True) as r:
            if r.status_code != 200:
                r.raise_for_status()
            logging.info("Uploading {} to Bucket={}, Key={}".format(file_name, bucket_name, key))
            with open("s3://{}/{}".format(bucket, key), "wb") as out:
                pool = ThreadPool(processes=10)
                pool.map(upload_chunk,
                         [{'chunk': chunk, 'out': out} for chunk in r.iter_content(chunk_size=chunk_size)])
                pool.close()
                pool.join()
        upload_end_time = datetime.utcnow()
        upload_duration = upload_end_time - upload_start_time
        upload_stats = {
            "file_name": file_name,
            "file_size (in bytes)": r.headers.get('Content-Length'),
            "upload_duration (in seconds)": upload_duration.total_seconds(),
            "upload_start_time": convert_datetime(upload_start_time),
            "upload_end_time": convert_datetime(upload_end_time)
        }
        return upload_stats
    except (ConnectionResetError, requests.exceptions.HTTPError) as e:
        return {"failed_download": e}


def upload_chunk(chunk_dict):
    logging.debug("Uploading {} byte(s)".format(len(chunk_dict['chunk'])))
    chunk_dict['out'].write(chunk_dict['chunk'])


def upload_url_list_from_s3(session, ES_CONN, downloads, args, job_id):
    aws_creds = get_aws_creds(session)
    s3 = boto3.Session(aws_access_key_id=aws_creds['accessKeyId'],
                       aws_secret_access_key=aws_creds['secretAccessKey'],
                       aws_session_token=aws_creds['sessionToken'],
                       region_name='us-west-2').client("s3")

    tmp_dir = "/tmp/data_subscriber"
    os.makedirs(tmp_dir, exist_ok=True)

    num_successes = num_failures = num_skipped = 0
    filtered_downloads = [f for f in downloads if "s3://" in f]

    for url in filtered_downloads:
        try:
            if ES_CONN.product_is_downloaded(url):
                logging.info(f"SKIPPING: {url}")
                num_skipped = num_skipped + 1
            else:
                if args.dry_run:
                    logging.info(f"{args.dry_run=}. Skipping downloads.")
                else:
                    result = s3_transfer(url, args.s3_bucket, s3, tmp_dir)
                    if "failed_download" in result:
                        raise Exception(result["failed_download"])
                    else:
                        logging.debug(str(result))

                ES_CONN.mark_product_as_downloaded(url, job_id)
                logging.info(f"{str(datetime.now())} SUCCESS: {url}")
                num_successes = num_successes + 1
        except Exception as e:
            logging.error(f"{str(datetime.now())} FAILURE: {url}")
            num_failures = num_failures + 1
            logging.error(e)

    logging.info(f"Files downloaded: {str(num_successes)}")
    logging.info(f"Duplicate files skipped: {str(num_skipped)}")
    logging.info(f"Files failed to download: {str(num_failures)}")

    shutil.rmtree(tmp_dir)


def get_aws_creds(session):
    with session.get("https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials") as r:
        if r.status_code != 200:
            r.raise_for_status()

        return r.json()


def s3_transfer(url, bucket_name, s3, tmp_dir, staging_area=""):
    file_name = os.path.basename(url)

    source = url[len("s3://"):].partition('/')
    source_bucket = source[0]
    source_key = source[2]

    target_bucket = bucket_name[len("s3://"):] if bucket_name.startswith("s3://") else bucket_name
    target_key = os.path.join(staging_area, file_name)

    try:
        s3.download_file(source_bucket, source_key, f"{tmp_dir}/{target_key}")

        target_s3 = boto3.resource("s3")
        target_s3.Bucket(target_bucket).upload_file(f"{tmp_dir}/{target_key}", target_key)

        return {"successful_download": target_key}
    except Exception as e:
        return {"failed_download": e}


@contextmanager
def token_ctx(token_url, ip_addr, edl):
    token = get_token(token_url, 'daac-subscriber', ip_addr, edl)
    try:
        yield token
    finally:
        delete_token(token_url, token)


def delete_token(url: str, token: str) -> None:
    try:
        headers = {'Content-Type': 'application/xml', 'Accept': 'application/json'}
        url = '{}/{}'.format(url, token)
        resp = requests.request('DELETE', url, headers=headers)
        if resp.status_code == 204:
            logging.info("CMR token successfully deleted")
        else:
            logging.warning("CMR token deleting failed.")
    except Exception as e:
        logging.error("Error deleting the token")
        raise e


if __name__ == '__main__':
    asyncio.run(run(sys.argv))
