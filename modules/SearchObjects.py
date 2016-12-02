import json
import logging
import random
import time

import multiprocessing
from collections import defaultdict
from datetime import datetime

from redislite import Redis

from common import Actions
from common.DataStructure import JSONSerializable
from common.ElasticsearchLoader import Loader
from common.ElasticsearchQuery import ESQuery
from common.LookupHelpers import LookUpDataRetriever, LookUpDataType
from common.Redis import RedisQueue, RedisQueueStatusReporter, RedisQueueWorkerProcess
from  multiprocessing import Process
from elasticsearch import Elasticsearch, helpers

from settings import Config



class SearchObjectActions(Actions):
    PROCESS = 'process'

class SearchObjectTypes(object):
    __ROOT__ = 'search_type'
    TARGET = 'target'
    DISEASE = 'disease'
    GO_TERM = 'go'
    PROTEIN_FEATURE = 'protein_feature'
    PUBLICATION = 'pub'
    SNP = 'snp'
    GENERIC = 'generic'

class SearchObject(JSONSerializable, object):
    """ Base class for search objects
    """
    def __init__(self,
                 id='',
                 name='',
                 full_name='',
                 description='',
                 ):
        self.id = id
        if not name:
            name = id
        self.name = name
        if not full_name:
            full_name = name
        self.full_name = full_name
        if not description:
            description = full_name
        self.description = description
        self.type = SearchObjectTypes.GENERIC
        self.private ={}
        self._create_suggestions()

    def set_associations(self,
                         top_associations =  dict(total = [], direct = []),
                         association_counts = dict(total = 0, direct = 0),
                         max_top = 20):
        self.top_associations = dict(total = top_associations['total'][:max_top],
                                     direct = top_associations['direct'][:max_top])
        self.association_counts = association_counts

    def _create_suggestions(self):
        '''reimplement in subclasses to allow a better autocompletion'''

        field_order = [self.id,
                       self.name,
                       self.description,
                       ]

        self.private['suggestions'] = dict(input = [],
                                           output = self.name,
                                           payload = dict(id = self.id,
                                                          title = self.name,
                                                          dull_name = self.full_name,
                                                          description = self.description),
                                           )


        for field in field_order:
            if isinstance(field, list):
                self.private['suggestions']['input'].extend(field)
            else:
                self.private['suggestions']['input'].append(field)

        self.private['suggestions']['input'] = [x.lower() for x in self.private['suggestions']['input']]

    def digest(self, json_input):
        pass

    def _parse_json(self, json_input):
        if isinstance(json_input, str) or isinstance(json_input, unicode):
            json_input=json.loads(json_input)
        return json_input


class SearchObjectTarget(SearchObject, object):
    """
    Target search object
    """
    def __init__(self,
                 id='',
                 name='',
                 description='',
                 ):
        super(SearchObjectTarget, self).__init__()
        self.type = SearchObjectTypes.TARGET
        self.ortholog = dict()

    def digest(self, json_input):
        json_input = self._parse_json(json_input)
        self.id=json_input['id']
        self.name=json_input['approved_symbol']
        self.full_name = json_input['approved_name']
        if json_input['uniprot_function']:
            self.description=json_input['uniprot_function'][0]
        self.approved_symbol=json_input['approved_symbol']
        self.approved_name=json_input['approved_name']
        self.symbol_synonyms=json_input['symbol_synonyms']
        self.name_synonyms=json_input['name_synonyms']
        self.biotype=json_input['biotype']
        self.gene_family_description=json_input['gene_family_description']
        self.uniprot_accessions=json_input['uniprot_accessions']
        self.hgnc_id=json_input['hgnc_id']
        self.ensembl_gene_id=json_input['ensembl_gene_id']
        if json_input['ortholog']:
            for species,ortholist in json_input['ortholog'].items():
                self.ortholog[species]=[
                        {'symbol': o["ortholog_species_symbol"],
                         'id':     o["ortholog_species_assert_ids"],
                         'name':   o["ortholog_species_name"]}
                        for o in ortholist
                ]
        if json_input['drugs']:
            self.drugs = json_input['drugs']
            self.drugs['drugbank'] = []
            for drug in json_input['drugbank']:
                if 'value' in drug and 'generic name' in drug['value']:
                    self.drugs['drugbank'].append(drug['value']['generic name'])



class SearchObjectDisease(SearchObject, object):
    """
    Target search object
    """
    def __init__(self,
                 id='',
                 name='',
                 description='',
                 ):
        super(SearchObjectDisease, self).__init__()
        self.type = SearchObjectTypes.DISEASE


    def digest(self, json_input):
        json_input = self._parse_json(json_input)
        self.id=json_input['path_codes'][0][-1]
        self.name=json_input['label']
        self.full_name=json_input['label']
        self.description=json_input['definition']
        self.efo_code=json_input['path_codes'][0][-1]
        self.efo_url=json_input['code']
        self.efo_label=json_input['label']
        self.efo_definition=json_input['definition']
        self.efo_synonyms=json_input['efo_synonyms']
        self.efo_path_codes=json_input['path_codes']
        self.efo_path_labels=json_input['path_labels']
        self.min_path_len=len(json_input['path_codes'][0])
        if len(json_input['path_codes'])>1:
            for path in json_input['path_codes'][1]:
                path_len = len(path)
                if path_len < self.min_path_len:
                    self.min_path_len = path_len
        # self.min_path_len-=1#correct for cttv_root
        self.phenotypes = json_input['phenotypes']


class SearchObjectAnalyserWorker(RedisQueueWorkerProcess):
    ''' read a list of objects from the processing queue and analyse them with the available association data before
    storing them in elasticsearch
    '''

    '''define data processing handlers'''
    data_handlers = defaultdict(lambda: SearchObject)
    data_handlers[SearchObjectTypes.TARGET] = SearchObjectTarget
    data_handlers[SearchObjectTypes.DISEASE] = SearchObjectDisease

    def __init__(self,
                 queue,
                 redis_path,
                 dry_run = False,
                 lookup = None):
        super(SearchObjectAnalyserWorker, self).__init__(queue,redis_path)
        self.queue = queue
        self.lookup = lookup
        self.loader = Loader(dry_run = dry_run)
        self.es_query = ESQuery(self.loader.es)
        # logging.info('%s started'%self.name)

    def process(self, data):
        '''process objects to simple search object'''
        so = self.data_handlers[data[SearchObjectTypes.__ROOT__]]()
        so.digest(json_input=data)
        '''inject drug data'''
        if not hasattr(so, 'drugs'):
            so.drugs = {}
        so.drugs['evidence_data'] = []
        '''count associations '''
        if data[SearchObjectTypes.__ROOT__] == SearchObjectTypes.TARGET:
            ass_data = self.es_query.get_associations_for_target(data['id'], fields=['id','harmonic-sum.overall'], size = 20)
            so.set_associations(self._summarise_association(ass_data.top_associations),
                                ass_data.associations_count)
            if so.id in self.lookup.chembl.target2molecule:
                drugs_synonyms = set()
                for molecule in self.lookup.chembl.target2molecule[so.id]:
                    drugs_synonyms = drugs_synonyms | set(self.lookup.chembl.molecule2synonyms[molecule])
                so.drugs['evidence_data'] = list(drugs_synonyms)

        elif data[SearchObjectTypes.__ROOT__] == SearchObjectTypes.DISEASE:
            ass_data = self.es_query.get_associations_for_disease(data['path_codes'][0][-1], fields=['id','harmonic-sum.overall'], size = 20)
            so.set_associations(self._summarise_association(ass_data.top_associations),
                                ass_data.associations_count)
            if so.id in self.lookup.chembl.disease2molecule:
                drugs_synonyms = set()
                for molecule in self.lookup.chembl.disease2molecule[so.id]:
                    drugs_synonyms = drugs_synonyms | set(self.lookup.chembl.molecule2synonyms[molecule])
                so.drugs['evidence_data'] = list(drugs_synonyms)
        else:
            so.set_associations()



        '''store search objects'''
        self.loader.put(Config.ELASTICSEARCH_DATA_SEARCH_INDEX_NAME,
                   Config.ELASTICSEARCH_DATA_SEARCH_DOC_NAME+'-'+so.type,
                   so.id,
                   so.to_json(),
                   create_index=False)

    def _summarise_association(self, data):
        def cap_score(value):
            if value >1:
                return 1.0
            elif value <-1:
                return -1
            return value
        return dict(total = [dict(id = data_point['id'],
                                score = cap_score(data_point['harmonic-sum']['overall'])) for data_point in data['total']],
                    direct = [dict(id = data_point['id'],
                                score = cap_score(data_point['harmonic-sum']['overall'])) for data_point in data['direct']]
            )




class SearchObjectProcess(object):
    def __init__(self,
                 loader,
                 r_server):
        self.loader = loader
        self.esquery = ESQuery(loader.es)
        self.r_server = r_server

    def process_all(self, dry_run = False):
        ''' process all the objects that needs to be returned by the search method
        :return:
        '''

        self.loader.create_new_index(Config.ELASTICSEARCH_DATA_SEARCH_INDEX_NAME)
        start_time = datetime.now()
        queue = RedisQueue(queue_id=Config.UNIQUE_RUN_ID+'|search_obj_processing',
                           max_size=1000,
                           job_timeout=180)

        q_reporter = RedisQueueStatusReporter([queue])
        q_reporter.start()
        lookup_data = LookUpDataRetriever(self.loader.es,self.r_server,data_types=[LookUpDataType.CHEMBL_DRUGS]).lookup
        workers = [SearchObjectAnalyserWorker(queue,
                                              self.r_server.db,
                                              lookup=lookup_data,
                                              dry_run=dry_run) for i in range(multiprocessing.cpu_count())]
        # workers = [SearchObjectAnalyserWorker(queue)]
        for w in workers:
            w.start()

        '''get gene simplified objects and push them to the processing queue'''
        for i,target in enumerate(self.esquery.get_all_targets()):
            target[SearchObjectTypes.__ROOT__] = SearchObjectTypes.TARGET
            queue.put(target, self.r_server)

        '''get disease objects  and push them to the processing queue'''
        for i,disease in enumerate(self.esquery.get_all_diseases()):
            disease[SearchObjectTypes.__ROOT__] = SearchObjectTypes.DISEASE
            queue.put(disease, self.r_server)

        queue.set_submission_finished(r_server=self.r_server)

        for w in workers:
            w.join()

        logging.info(queue.get_status(r_server=self.r_server))
        logging.info('ALL DONE! Execution time: %s'%(datetime.now()-start_time))
