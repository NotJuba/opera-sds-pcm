from datetime import datetime

from hysds_commons.elasticsearch_utils import ElasticsearchUtility

ES_INDEX = "data_subscriber_product_catalog"


class DataSubscriberProductCatalog(ElasticsearchUtility):
    """
    Class to track products downloaded by daac_data_subscriber.py

    https://github.com/hysds/hysds_commons/blob/develop/hysds_commons/elasticsearch_utils.py
    ElasticsearchUtility methods
        index_document
        get_by_id
        query
        search
        get_count
        delete_by_id
        update_document
    """

    def create_index(self, index=ES_INDEX):
        self.es.indices.create(body={"settings": {"index": {"sort.field": "index_datetime", "sort.order": "asc"}},
                                     "mappings": {
                                         "properties": {
                                             "url": {"type": "keyword"},
                                             "index_datetime": {"type": "date"},
                                             "download_datetime": {"type": "date"},
                                             "downloaded": {"type": "boolean"}}}},
                               index=ES_INDEX)
        if self.logger:
            self.logger.info("Successfully created index: {}".format(index))

    def delete_index(self, index=ES_INDEX):
        self.es.indices.delete(index=index, ignore=404)
        if self.logger:
            self.logger.info("Successfully deleted index: {}".format(index))

    def post(self, id, url, index=ES_INDEX):
        result = self.index_document(index=index,
                                     body={"url": url, "downloaded": False, "index_datetime": datetime.now()},
                                     id=id)

        if self.logger:
            self.logger.debug(f"Document indexed: {result}")

    def mark_downloaded(self, id, index=ES_INDEX):
        result = self.update_document(id=id,
                                      body={"doc_as_upsert": True,
                                            "doc": {"downloaded": True, "download_datetime": datetime.now()}},
                                      index=index)

        if self.logger:
            self.logger.debug(f"Document updated: {result}")

    def query_existence(self, id, index=ES_INDEX):
        try:
            result = self.get_by_id(index=index, id=id)
            if self.logger:
                self.logger.debug(f"Query result: {result}")

        except:
            result = None
            if self.logger:
                self.logger.debug(f"{id} does not exist in {index}")

        return result

    def query_undownloaded(self, index=ES_INDEX):
        try:
            result = self.query(index=index,
                                body={"sort": [{"index_datetime": "asc"}], "query": {"match": {"downloaded": False}}})
            if self.logger:
                self.logger.debug(f"Query result: {result}")

        except:
            result = None
            if self.logger:
                self.logger.debug(f"{id} does not exist in {index}")

        return result
