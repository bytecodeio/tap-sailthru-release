import csv
import datetime
import time
from datetime import timedelta
from typing import Any, Iterator

import requests
import singer
from singer import Transformer, metrics

from tap_sailthru.client import SailthruClient
from tap_sailthru.client.error import SailthruJobTimeout
from tap_sailthru.transform import (advance_date_by_microsecond,
                                    flatten_user_response,
                                    get_purchase_key_type,
                                    get_start_and_end_date_params,
                                    rfc2822_to_datetime, sort_by_rfc2822)

LOGGER = singer.get_logger()

class BaseStream:
    """
    A base class representing singer streams.

    :param client: The API client used extract records from the external source
    """
    object_type = None
    tap_stream_id = None
    replication_method = None
    replication_key = None
    key_properties = []
    valid_replication_keys = []
    params = {}
    parent = None

    def __init__(self, client: SailthruClient):
        self.client = client

    def get_records(self, bookmark_datetime: datetime = None, is_parent: bool = False) -> list:
        """
        Returns a list of records for that stream.

        :param bookmark_datetime: The datetime object representing the bookmark date
        :param is_parent: If true, may change the type of data
            that is returned for a child stream to consume
        :return: list of records
        """
        raise NotImplementedError("Child classes of BaseStream require `get_records` implementation")

    def get_parent_data(self, bookmark_datetime: datetime = None) -> list:
        """
        Returns a list of records from the parent stream.

        :param bookmark_datetime: The datetime object representing the bookmark date
        :return: A list of records
        """
        parent = self.parent(self.client)
        return parent.get_records(bookmark_datetime, is_parent=True)

    def post_job(self, parameter: Any = None) -> dict:
        """
        Creates a data export background job. More details:
        https://getstarted.sailthru.com/developers/api/job/

        :param parameter: Any parameter type to be passed to the logger
        :return: The API response as a dictionary
        """
        job_name = self.params.get('job')
        if parameter:
            LOGGER.info(f'Starting background job for {job_name}, parameter={parameter}')
        else:
            LOGGER.info(f'Starting background job for {job_name}')
        return self.client.create_job(self.params).get_body()

    def get_job_url(self, job_id: str, timeout: int = 600) -> str:
        """
        Polls the /job endpoint and checks to see if export job is completed.
        Returns the export URL when job is ready.

        :param job_id: the job_id to poll
        :param timeout: the default timeout (seconds) before halting request
        :return: the export URL
        """
        status = ''
        job_start_time = singer.utils.now()
        while status != 'completed':
            response = self.client.get_job(job_id).get_body()
            status = response.get('status')
            LOGGER.info(f'Job report status: {status}')
            now = singer.utils.now()
            if (now - job_start_time).seconds > timeout:
                LOGGER.critical(f"Request with job_id {job_id} exceeded {timeout} second timeout")
                raise SailthruJobTimeout
            time.sleep(1)

        return response.get('export_url')

    @staticmethod
    def process_job_csv(export_url: str, chunk_size: int = 1024) -> Iterator[dict]:
        """
        Fetches CSV from URL and streams each line.

        :param export_url: The URL from which to fetch the CSV data from
        :param chunk_size: The chunk size to read per line
        :return: A generator of a dictionary
        """
        with requests.get(export_url, stream=True) as r:
            reader = csv.DictReader(line.decode('utf-8') for line in r.iter_lines(chunk_size=chunk_size))
            for row in reader:
                yield row


class IncrementalStream(BaseStream):
    """
    A child class of a base stream used to represent streams that use the
    INCREMENTAL replication method.

    :param client: The API client used extract records from the external source
    """
    replication_method = 'INCREMENTAL'

    def __init__(self, client):
        super().__init__(client)

    def sync(self, state: dict, stream_schema: dict, stream_metadata: dict, config: dict, transformer: Transformer) -> dict:
        """
        The sync logic for an incremental stream.

        :param state: A dictionary representing singer state
        :param stream_schema: A dictionary containing the stream schema
        :param stream_metadata: A dictionnary containing stream metadata
        :param config: A dictionary containing tap config data
        :param transformer: A singer Transformer object
        :return: State data in the form of a dictionary
        """
        start_time = singer.get_bookmark(state, self.tap_stream_id, self.replication_key, config['start_date'])
        # Since records can contain the same 'modify_time' timestamp due to batch uploads
        # we need to use >= to compare and write records and in order to avoid re-syncing
        # records from the previous run, we add a microsecond to the start date
        bookmark_date = advance_date_by_microsecond(start_time)
        bookmark_datetime = singer.utils.strptime_to_utc(bookmark_date)
        max_datetime = bookmark_datetime

        with metrics.record_counter(self.tap_stream_id) as counter:
            for record in self.get_records(bookmark_datetime):
                record_datetime = rfc2822_to_datetime(record[self.replication_key])
                if record_datetime >= bookmark_datetime:
                    transformed_record = transformer.transform(record, stream_schema, stream_metadata)
                    singer.write_record(self.tap_stream_id, transformed_record)
                    counter.increment()
                    max_datetime = max(record_datetime, max_datetime)
            bookmark_date = singer.utils.strftime(max_datetime)

        state = singer.write_bookmark(state, self.tap_stream_id, self.replication_key, bookmark_date)
        return state


class FullTableStream(BaseStream):
    """
    A child class of a base stream used to represent streams that use the
    FULL_TABLE replication method.

    :param client: The API client used extract records from the external source
    """
    replication_method = 'FULL_TABLE'

    def __init__(self, client):
        super().__init__(client)

    def sync(self, state: dict, stream_schema: dict, stream_metadata: dict, config: dict, transformer: Transformer) -> dict:
        """
        The sync logic for an full table stream.

        :param state: A dictionary representing singer state
        :param stream_schema: A dictionary containing the stream schema
        :param stream_metadata: A dictionnary containing stream metadata
        :param config: A dictionary containing tap config data
        :param transformer: A singer Transformer object
        :return: State data in the form of a dictionary
        """
        with metrics.record_counter(self.tap_stream_id) as counter:
            for record in self.get_records():
                transformed_record = transformer.transform(record, stream_schema, stream_metadata)
                singer.write_record(self.tap_stream_id, transformed_record)
                counter.increment()

        return state


class AdTargeterPlans(FullTableStream):
    """
    Gets records for Sailthru Ad Targeter Plans.

    Docs: https://getstarted.sailthru.com/developers/api/ad-plan/
    """
    tap_stream_id = 'ad_targeter_plans'
    key_properties = ['plan_id']

    def get_records(self, bookmark_datetime=None, is_parent=False):
        response = self.client.get_ad_targeter_plans().get_body()
        yield from response['ad_plans']


class Blasts(IncrementalStream):
    """
    Gets records for blasts (aka Sailtrhu campaigns).

    Docs: https://getstarted.sailthru.com/developers/api/blast/

    This endpoint does not have a method of retrieving all blasts available.
    You can only retrieve blast information by specifying a blast_id or you
    can get all blasts by status type (sent, sending, scheduled, unscheduled).
    """
    tap_stream_id = 'blasts'
    key_properties = ['blast_id']
    replication_key = 'modify_time'
    valid_replication_keys = ['modify_time']
    params = {
        'statuses': ['sent', 'sending', 'unscheduled', 'scheduled'],
    }

    # TODO: investigate 403 response code
    def get_records(self, bookmark_datetime=None, is_parent=False):
        # Will just return a list of blast_id if being called
        # by child stream
        if is_parent:
            blast_ids = []
            for status in self.params['statuses']:
                response = self.client.get_blasts(status).get_body()
                ids = [blast.get('blast_id') for blast in response['blasts']]
                blast_ids.extend(ids)

            yield from blast_ids
        else:
            # TODO: Need to figure out how to sort these
            blasts = []
            for status in self.params['statuses']:
                response = self.client.get_blasts(status).get_body()
                # Add the blast status to each blast record
                response['blasts'] = [dict(item, status=status) for item in response['blasts']]
                blasts.extend(response['blasts'])

            sorted_blasts = sort_by_rfc2822(blasts, 'modify_time')

            yield from sorted_blasts

class BlastQuery(FullTableStream):
    """
    Triggers asynchronous data export job for blasts and then fetches the
    records when the job is done.

    Docs: https://getstarted.sailthru.com/developers/api/job/#blast-query
    """
    tap_stream_id = 'blast_query'
    key_properties = ['profile_id']
    params = {
        'job': 'blast_query',
        'blast_id': '{blast_id}',
    }
    parent = Blasts

    def get_records(self, bookmark_datetime=None, is_parent=False):

        for blast_id in self.get_parent_data():
            self.params['blast_id'] = blast_id

            response = self.post_job(parameter=self.params['blast_id'])
            try:
                export_url = self.get_job_url(job_id=response['job_id'])
            except Exception as e:
                if response.get("error"):
                    # https://getstarted.sailthru.com/developers/api/job/#Error_Codes
                    # Error code 99 = You may not export a blast that has been sent
                    if response.get("error") == 99:
                        LOGGER.warn(f"{response.get('errormsg')}")
                        LOGGER.info(f"Skipping blast_id: {self.params['blast_id']}")
                        continue
                    else:
                        LOGGER.exception(e)
                        raise e
            LOGGER.info(f'export_url: {export_url}')

            yield from self.process_job_csv(export_url=export_url)


class BlastRepeats(IncrementalStream):
    """
    Gets all recurring campaigns.

    Docs: https://getstarted.sailthru.com/developers/api/blast_repeat/
    """
    tap_stream_id = 'blast_repeats'
    key_properties = ['repeat_id']
    replication_key = 'modify_time'
    valid_replication_keys = ['modify_time']

    def get_records(self, bookmark_datetime=None, is_parent=False):
        response = self.client.get_blast_repeats().get_body()
        repeats = response['repeats']
        # Sort repeats by 'modify_time' field
        sorted_repeats = sort_by_rfc2822(repeats, sort_key='modify_time')

        yield from sorted_repeats


class Lists(FullTableStream):
    """
    Retrieves info for Sailthru lists.

    Docs: https://getstarted.sailthru.com/developers/api/list/
    """
    tap_stream_id = 'lists'
    key_properties = ['list_id']

    # TODO: might be good candiate for lru
    def get_records(self, bookmark_datetime=None, is_parent=False):
        # Will just return list names if called by child stream
        if is_parent:
            response = self.client.get_lists().get_body()
            for list in response['lists']:
                yield list['name']
        else:
            response = self.client.get_lists().get_body()
            yield from response['lists']


class BlastSaveList(FullTableStream):
    """
    Triggers asynchronous data export job for blast_save_query and then
    fetches the records when the job is done.

    Docs: https://getstarted.sailthru.com/developers/api/job/#blast-save-list
    """
    tap_stream_id = 'blast_save_list'
    key_properties = ['Profile Id']
    params = {
        'job': 'export_list_data',
        'list': '{list_name}',
    }
    parent = Lists

    def get_records(self, bookmark_datetime=None, is_parent=False):

        for list_name in self.get_parent_data():
            self.params['list'] = list_name

            response = self.post_job(parameter=self.params['list'])
            export_url = self.get_job_url(job_id=response['job_id'])
            LOGGER.info(f'export_url: {export_url}')

            yield from self.process_job_csv(export_url=export_url)


class Users(FullTableStream):
    """
    Retrieve user profile data.

    Docs: https://getstarted.sailthru.com/developers/api/user/
    """
    tap_stream_id = 'users'
    key_properties = ['profile_id']
    params = {
        'id': '{profile_id}',
        'key': 'sid',
    }
    parent = BlastSaveList

    def get_records(self):

        for record in self.get_parent_data():
            profile_id = record['Profile Id']
            response = self.client.get_user(profile_id).get_body()
            yield flatten_user_response(response)


class PurchaseLog(IncrementalStream):
    """
    Triggers asynchronous data export job for export_purchase_log and then
    fetches the records when the job is done.

    Docs: https://getstarted.sailthru.com/developers/api/job/#export-purchase-log
    """
    tap_stream_id = 'purchase_log'
    key_properties = ['purchase_id']
    replication_key = 'Date'
    valid_replication_keys = ['Date']
    params = {
        'job': 'export_purchase_log',
        'start_date': '{purchase_log_start_date}',
        'end_date': '{purchase_log_end_date}',
    }

    def get_records(self, bookmark_datetime=None, is_parent=False):

        start_datetime, end_datetime = get_start_and_end_date_params(bookmark_datetime)
        now = singer.utils.now()

        # Generate a report for each day up until the end date or today's date
        while start_datetime.date() < min(end_datetime.date(), now.date()):

            job_date = start_datetime.strftime('%Y%m%d')
            self.params['start_date'] = job_date
            self.params['end_date'] = job_date

            response = self.post_job(parameter=(self.params['start_date'],
                                     self.params['end_date']))
            export_url = self.get_job_url(job_id=response['job_id'])
            LOGGER.info(f'export_url: {export_url}')

            records = sort_by_rfc2822(
                        self.process_job_csv(export_url=export_url), 'Date')
            for record in records:
                # Purchase key could be Extid or Sid
                purchase_key = get_purchase_key_type(record)
                # Add purchase_key field
                record.update({'purchase_key': purchase_key})

                yield record

            start_datetime += timedelta(days=1)


class Purchases(IncrementalStream):
    """
    Retrieve data on a purchase by the purchase key type (sid vs extid).

    Docs: https://getstarted.sailthru.com/developers/api/purchase/
    """
    tap_stream_id = 'purchases'
    key_properties = ['item_id']
    replication_key = 'time'
    valid_replication_keys = ['time']
    parent = PurchaseLog
    batched = True

    def get_records(self, bookmark_datetime=None, is_parent=False):

        for record in self.get_parent_data(bookmark_datetime):
            purchase_key = record.get('purchase_key')
            purchase_id = record.get(purchase_key)
            if not purchase_id:
                LOGGER.warn("No purchase_id found for record")
                continue
            # TODO: sort responses
            response = self.client.get_purchase(purchase_id, purchase_key=purchase_key.lower()).get_body()

            if response.get("error"):
                LOGGER.warn(f"error with record: {response['error']}")
                continue

            yield response

STREAMS = {
    'ad_targeter_plans': AdTargeterPlans,
    'blasts': Blasts,
    'blast_query': BlastQuery,
    'blast_repeats': BlastRepeats,
    'lists': Lists,
    'blast_save_list': BlastSaveList,
    'users': Users,
    'purchase_log': PurchaseLog,
    'purchases': Purchases,
}
