import re
import functools

import six
import sqlalchemy as sa

from collections import defaultdict

from datetime import date


from sqlalchemy.orm import foreign
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.dialects import postgresql


from webservices.env import env
from elasticsearch import Elasticsearch

import flask_restful as restful
from marshmallow_pagination import paginators

from webargs import fields
from flask_apispec import use_kwargs as use_kwargs_original
from flask_apispec.views import MethodResourceMeta

from webservices import docs
from webservices import sorting
from webservices import decoders
from webservices import exceptions


use_kwargs = functools.partial(use_kwargs_original, locations=('query', ))


class Resource(six.with_metaclass(MethodResourceMeta, restful.Resource)):
    pass

API_KEY_ARG = fields.Str(
    required=True,
    missing='DEMO_KEY',
    description=docs.API_KEY_DESCRIPTION,
)
if env.get_credential('PRODUCTION'):
    Resource = use_kwargs({'api_key': API_KEY_ARG})(Resource)

fec_url_map = {'9': 'http://docquery.fec.gov/dcdev/posted/{0}.fec'}
fec_url_map = defaultdict(lambda : 'http://docquery.fec.gov/paper/posted/{0}.fec', fec_url_map)


def check_cap(kwargs, cap):
    if cap:
        if not kwargs.get('per_page') or kwargs['per_page'] > cap:
            raise exceptions.ApiError(
                'Parameter "per_page" must be between 1 and {}'.format(cap),
                status_code=422,
            )


def fetch_page(query, kwargs, model=None, aliases=None, join_columns=None, clear=False,
               count=None, cap=100, index_column=None, multi=False):
    check_cap(kwargs, cap)
    sort, hide_null, reverse_nulls = kwargs.get('sort'), kwargs.get('sort_hide_null'), kwargs.get('sort_reverse_nulls')
    if sort and multi:
        query, _ = sorting.multi_sort(
            query, sort, model=model, aliases=aliases, join_columns=join_columns,
            clear=clear, hide_null=hide_null, index_column=index_column
        )
    elif sort:
        query, _ = sorting.sort(
            query, sort, model=model, aliases=aliases, join_columns=join_columns,
            clear=clear, hide_null=hide_null, index_column=index_column
        )
    paginator = paginators.OffsetPaginator(query, kwargs['per_page'], count=count)
    return paginator.get_page(kwargs['page'])

class SeekCoalescePaginator(paginators.SeekPaginator):

    def __init__(self, cursor, per_page, index_column, sort_column=None, count=None):
        self.max_column_map = {
            "date": date.max,
            "float": float("inf"),
            "int": float("inf")
        }
        self.min_column_map = {
            "date": date.min,
            "float": float("inf"),
            "int": float("inf")
        }
        super(SeekCoalescePaginator, self).__init__(cursor, per_page, index_column, sort_column, count)


    def _fetch(self, last_index, sort_index=None, limit=None, eager=True):
        cursor = self.cursor
        direction = self.sort_column[1] if self.sort_column else sa.asc
        lhs, rhs = (), ()
        if sort_index is not None:
            left_index = self.sort_column[0]
            comparator = self.max_column_map.get(str(left_index.property.columns[0].type).lower())
            left_index = sa.func.coalesce(left_index, comparator)
            lhs += (left_index,)
            rhs += (sort_index,)
        if last_index is not None:
            lhs += (self.index_column,)
            rhs += (last_index,)
        lhs = sa.tuple_(*lhs)
        rhs = sa.tuple_(*rhs)
        if rhs.clauses:
            filter = lhs > rhs if direction == sa.asc else lhs < rhs
            cursor = cursor.filter(filter)
        query = cursor.order_by(direction(self.index_column)).limit(limit)
        return query.all() if eager else query

    def _get_index_values(self, result):
        """Get index values from last result, to be used in seeking to the next
        page. Optionally include sort values, if any.
        """
        ret = {'last_index': paginators.convert_value(result, self.index_column)}
        if self.sort_column:
            key = 'last_{0}'.format(self.sort_column[0].key)
            ret[key] = paginators.convert_value(result, self.sort_column[0])
            if ret[key] is None:
                ret.pop(key)
                ret['sort_null_only'] = True
        return ret


def fetch_seek_page(query, kwargs, index_column, clear=False, count=None, cap=100, eager=True):
    paginator = fetch_seek_paginator(query, kwargs, index_column, clear=clear, count=count, cap=cap)
    if paginator.sort_column is not None:
        sort_index = kwargs['last_{0}'.format(paginator.sort_column[0].key)]
        if not sort_index and kwargs['sort_null_only'] and paginator.sort_column[1] == sa.asc:
            sort_index = None
            query = query.filter(paginator.sort_column[0] == None)
            paginator.cursor = query
    else:
        sort_index = None
    return paginator.get_page(last_index=kwargs['last_index'], sort_index=sort_index, eager=eager)


def fetch_seek_paginator(query, kwargs, index_column, clear=False, count=None, cap=100):
    check_cap(kwargs, cap)
    model = index_column.parent.class_
    sort, hide_null = kwargs.get('sort'), kwargs.get('sort_hide_null')
    if sort:
        query, sort_column = sorting.sort(
            query, sort,
            model=model, clear=clear, hide_null=hide_null
        )
    else:
        sort_column = None
    return SeekCoalescePaginator(
        query,
        kwargs['per_page'],
        index_column,
        sort_column=sort_column,
        count=count,
    )


def extend(*dicts):
    ret = {}
    for each in dicts:
        ret.update(each)
    return ret


def parse_fulltext(text):
    return ' & '.join([
        part + ':*'
        for part in re.sub(r'\W', ' ', text).split()
    ])


office_args_required = ['office', 'cycle']
office_args_map = {
    'house': ['state', 'district'],
    'senate': ['state'],
}
def check_election_arguments(kwargs):
    for arg in office_args_required:
        if kwargs.get(arg) is None:
            raise exceptions.ApiError(
                'Required parameter "{0}" not found.'.format(arg),
                status_code=422,
            )
    conditional_args = office_args_map.get(kwargs['office'], [])
    for arg in conditional_args:
        if kwargs.get(arg) is None:
            raise exceptions.ApiError(
                'Must include argument "{0}" with office type "{1}"'.format(
                    arg,
                    kwargs['office'],
                ),
                status_code=422,
            )


def get_model(name):
    from webservices.common.models import db
    return db.Model._decl_class_registry.get(name)


def related(related_model, id_label, related_id_label=None, cycle_label=None,
            related_cycle_label=None, use_modulus=True):
    from webservices.common.models import db
    related_model = get_model(related_model)
    related_id_label = related_id_label or id_label
    related_cycle_label = related_cycle_label or cycle_label
    @declared_attr
    def related(cls):
        id_column = getattr(cls, id_label)
        related_id_column = getattr(related_model, related_id_label)
        filters = [foreign(id_column) == related_id_column]
        if cycle_label:
            cycle_column = getattr(cls, cycle_label)
            if use_modulus:
                cycle_column = cycle_column + cycle_column % 2
            related_cycle_column = getattr(related_model, related_cycle_label)
            filters.append(cycle_column == related_cycle_column)
        return db.relationship(
            related_model,
            primaryjoin=sa.and_(*filters),
        )
    return related


related_committee = functools.partial(related, 'CommitteeDetail', 'committee_id')
related_candidate = functools.partial(related, 'CandidateDetail', 'candidate_id')

related_committee_history = functools.partial(
    related,
    'CommitteeHistory',
    'committee_id',
    related_cycle_label='cycle',
)
related_candidate_history = functools.partial(
    related,
    'CandidateHistory',
    'candidate_id',
    related_cycle_label='two_year_period',
)
related_efile_summary = functools.partial(
    related,
    'EFilings',
    'file_number',
    related_id_label='file_number',
)

def document_description(report_year, report_type=None, document_type=None, form_type=None):
    if report_type:
        clean = re.sub(r'\{[^)]*\}', '', report_type)
    elif document_type:
        clean = document_type
    elif form_type and form_type in decoders.form_types:
        clean = decoders.form_types[form_type]
    else:
        clean = 'Document'

    if form_type and (form_type == 'RFAI' or form_type == 'FRQ'):
        clean = 'RFAI: ' + clean
    return '{0} {1}'.format(clean.strip(), report_year)


def make_report_pdf_url(image_number):
    if image_number:
        return 'http://docquery.fec.gov/pdf/{0}/{1}/{1}.pdf'.format(
            str(image_number)[-3:],
            image_number,
        )
    else:
        return None

def make_schedule_pdf_url(image_number):
    if image_number:
        return 'http://docquery.fec.gov/cgi-bin/fecimg/?' + image_number


def make_csv_url(file_num):
    file_number = str(file_num)
    if file_num > -1 and file_num < 100:
        return 'http://docquery.fec.gov/csv/000/{0}.csv'.format(file_number)
    elif file_num >= 100:
        return 'http://docquery.fec.gov/csv/{0}/{1}.csv'.format(file_number[-3:], file_number)

def make_fec_url(image_number, file_num):
    image_number = str(image_number)
    if file_num < 0 or file_num is None:
        return
    file_num = str(file_num)
    indicator = -1
    if len(image_number) == 18:
        indicator = image_number[8]
    elif len(image_number) == 11:
        indicator = image_number[2]
    return fec_url_map[indicator].format(file_num)

def get_index_column(model):
    column = model.__mapper__.primary_key[0]
    return getattr(model, column.key)


def cycle_param(**kwargs):
    ret = {
        'name': 'cycle',
        'type': 'integer',
        'in': 'path',
    }
    ret.update(kwargs)
    return ret


def get_election_duration(column):
    return sa.case(
        [
            (column == 'S', 6),
            (column == 'P', 4),
        ],
        else_=2,
    )

def get_elasticsearch_connection():
    es_conn = env.get_service(name='fec-api-search')
    if es_conn:
        es = Elasticsearch([es_conn.get_url(url='uri')])
    else:
        es = Elasticsearch(['http://localhost:9200'])
    return es

def print_literal_query_string(query):
    print(str(query.statement.compile(dialect=postgresql.dialect())))

def create_eregs_link(part, section):
    url_part_section = part
    if section:
        url_part_section += '-' + section
    return '/regulations/{}/CURRENT'.format(url_part_section)
