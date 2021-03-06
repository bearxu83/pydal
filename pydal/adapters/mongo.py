# -*- coding: utf-8 -*-
import datetime
import re

from .._globals import IDENTITY
from .._compat import integer_types, basestring
from ..objects import Table, Query, Field, Expression
from ..helpers.classes import SQLALL, Reference
from ..helpers.methods import use_common_filters, xorify
from .base import NoSQLAdapter
try:
    from bson import Binary
    from bson.binary import USER_DEFINED_SUBTYPE
except:
    class Binary(object):
        pass
    USER_DEFINED_SUBTYPE = 0

long = integer_types[-1]


class MongoDBAdapter(NoSQLAdapter):
    drivers = ('pymongo',)
    driver_auto_json = ['loads', 'dumps']

    uploads_in_blob = False

    types = {
        'boolean': bool,
        'string': str,
        'text': str,
        'json': str,
        'password': str,
        'blob': str,
        'upload': str,
        'integer': long,
        'bigint': long,
        'float': float,
        'double': float,
        'date': datetime.date,
        'time': datetime.time,
        'datetime': datetime.datetime,
        'id': long,
        'reference': long,
        'list:string': list,
        'list:integer': list,
        'list:reference': list,
    }

    error_messages = {"javascript_needed": "This must yet be replaced" +
                      " with javascript in order to work."}

    def __init__(self, db, uri='mongodb://127.0.0.1:5984/db',
                 pool_size=0, folder=None, db_codec='UTF-8',
                 credential_decoder=IDENTITY, driver_args={},
                 adapter_args={}, do_connect=True, after_connection=None):

        super(MongoDBAdapter, self).__init__(
            db=db,
            uri=uri,
            pool_size=pool_size,
            folder=folder,
            db_codec=db_codec,
            credential_decoder=credential_decoder,
            driver_args=driver_args,
            adapter_args=adapter_args,
            do_connect=do_connect,
            after_connection=after_connection)

        if do_connect: self.find_driver(adapter_args)
        import random
        from bson.objectid import ObjectId
        from bson.son import SON
        import pymongo.uri_parser
        from pymongo.write_concern import WriteConcern

        m = pymongo.uri_parser.parse_uri(uri)

        self.SON = SON
        self.ObjectId = ObjectId
        self.random = random
        self.WriteConcern = WriteConcern

        self.dbengine = 'mongodb'
        db['_lastsql'] = ''
        self.db_codec = 'UTF-8'
        self.find_or_make_work_folder()
        #this is the minimum amount of replicates that it should wait
        # for on insert/update
        self.minimumreplication = adapter_args.get('minimumreplication', 0)
        # by default all inserts and selects are performand asynchronous,
        # but now the default is
        # synchronous, except when overruled by either this default or
        # function parameter
        self.safe = 1 if adapter_args.get('safe', True) else 0

        # Aggregations require a slightly different syntax for identifiers.
        # This is a bit of hack to add state information to backend for the
        # parser.  (ie: expand())  The parser backend is VERY intimately tied
        # to the adapter.  When making these changes only found one state
        # variable relative to the backend (_colnames).
        # 
        # Don't fully understand the threading model this adapter is expected
        # to run under so went this conservative route to avoid state
        # information from the parse being stored in the adapter. If the adapter
        # is never expected to run in a multi thread environment, then this bit
        # could be very slightly simpler, if not then _colnames is likely a bug. 
        self.aggregate = False
        self.aggregate_expander = MongoDbAggregateExpander()
        self.aggregate_expander.ObjectId = ObjectId
        def aggregate_expand(self, expression, field_type=None):
            self.aggregate_expander.expand(expression, field_type)
        self.expand_aggregate = self.aggregate_expander.expand

        if isinstance(m, tuple):
            m = {"database": m[1]}
        if m.get('database') is None:
            raise SyntaxError("Database is required!")

        def connector(uri=self.uri, m=m):
            driver = self.driver.MongoClient(uri, w=self.safe)[m.get('database')]
            driver.cursor = lambda : self.fake_cursor
            driver.close = lambda : None
            driver.commit = lambda : None
            return driver
        self.connector = connector
        self.reconnect()

        # _server_version is a string like '3.0.3' or '2.4.12'
        self._server_version = self.connection.command("serverStatus")['version']
        self.server_version = tuple(
            [int(x) for x in self._server_version.split('.')])
        self.server_version_major = (
            self.server_version[0] + self.server_version[1] / 10.0)

    def object_id(self, arg=None):
        """ Convert input to a valid Mongodb ObjectId instance

        self.object_id("<random>") -> ObjectId (not unique) instance """
        if not arg:
            arg = 0
        if isinstance(arg, basestring):
            # we assume an integer as default input
            rawhex = len(arg.replace("0x", "").replace("L", "")) == 24
            if arg.isdigit() and (not rawhex):
                arg = int(arg)
            elif arg == "<random>":
                arg = int("0x%sL" % \
                "".join([self.random.choice("0123456789abcdef") \
                for x in range(24)]), 0)
            elif arg.isalnum():
                if not arg.startswith("0x"):
                    arg = "0x%s" % arg
                try:
                    arg = int(arg, 0)
                except ValueError as e:
                    raise ValueError(
                            "invalid objectid argument string: %s" % e)
            else:
                raise ValueError("Invalid objectid argument string. " +
                                 "Requires an integer or base 16 value")
        elif isinstance(arg, self.ObjectId):
            return arg

        if not isinstance(arg, (int, long)):
            raise TypeError("object_id argument must be of type " +
                            "ObjectId or an objectid representable integer")
        hexvalue = hex(arg)[2:].rstrip('L').zfill(24)
        return self.ObjectId(hexvalue)

    def parse_reference(self, value, field_type):
        # here we have to check for ObjectID before base parse
        if isinstance(value, self.ObjectId):
            value = long(str(value), 16)
        return super(MongoDBAdapter,
                     self).parse_reference(value, field_type)

    def parse_id(self, value, field_type):
        if isinstance(value, self.ObjectId):
            value = long(str(value), 16)
        return super(MongoDBAdapter,
                     self).parse_id(value, field_type)

    def represent(self, obj, fieldtype):
        # the base adapter does not support MongoDB ObjectId
        if isinstance(obj, self.ObjectId):
            value = obj
        else:
            value = NoSQLAdapter.represent(self, obj, fieldtype)
        # reference types must be convert to ObjectID
        if fieldtype  =='date':
            if value is None:
                return value
            # this piece of data can be stripped off based on the fieldtype
            t = datetime.time(0, 0, 0)
            # mongodb doesn't has a date object and so it must datetime,
            # string or integer
            return datetime.datetime.combine(value, t)
        elif fieldtype == 'time':
            if value is None:
                return value
            # this piece of data can be stripped of based on the fieldtype
            d = datetime.date(2000, 1, 1)
            # mongodb doesn't has a  time object and so it must datetime,
            # string or integer
            return datetime.datetime.combine(d, value)
        elif fieldtype == "blob":
            return MongoBlob(value)
        elif isinstance(fieldtype, basestring):
            if fieldtype.startswith('list:'):
                if fieldtype.startswith('list:reference'):
                    value = [self.object_id(v) for v in value]
            elif fieldtype.startswith("reference") or fieldtype=="id":
                value = self.object_id(value)
        elif isinstance(fieldtype, Table):
            value = self.object_id(value)
        return value

    def parse_blob(self, value, field_type):
        return MongoBlob.decode(value)

    def _expand_query(self, query, tablename=None, safe=None):
        """ Return a tuple containing query and ctable """
        if not tablename:
            tablename = self.get_table(query)
        ctable = self._get_collection(tablename, safe)
        _filter = None
        if query:
            if use_common_filters(query):
                query = self.common_filter(query,[tablename])
            _filter = self.expand(query)
        return (ctable, _filter)

    def _get_collection(self, tablename, safe=None):
        ctable = self.connection[tablename]

        if safe is not None and safe != self.safe:
            wc = self.WriteConcern(w=self._get_safe(safe))
            ctable = ctable.with_options(write_concern=wc)

        return ctable

    def _get_safe(self, val=None):
        if val is None:
            return self.safe
        return 1 if val else 0

    def create_table(self, table, migrate=True, fake_migrate=False,
                     polymodel=None, isCapped=False):
        if isCapped:
            raise RuntimeError("Not implemented")
        table._dbt = None

    def expand(self, expression, field_type=None):
        if isinstance(expression, Query):
            # any query using 'id':=
            # set name as _id (as per pymongo/mongodb primary key)
            # convert second arg to an objectid field
            # (if its not already)
            # if second arg is 0 convert to objectid
            if isinstance(expression.first,Field) and \
                    ((expression.first.type == 'id') or \
                    ("reference" in expression.first.type)):
                if expression.first.type == 'id':
                    expression.first.name = '_id'
                # cast to Mongo ObjectId
                if isinstance(expression.second, (tuple, list, set)):
                    expression.second = [self.object_id(item) for
                                         item in expression.second]
                else:
                    expression.second = self.object_id(expression.second)

        if isinstance(expression, Field):
            if expression.type=='id':
                result = "_id"
            else:
                result = expression.name
                if self.aggregate:
                    result = '$' + result

        elif isinstance(expression, (Expression, Query)):
            first = expression.first
            second = expression.second
            op = expression.op
            optional_args = expression.optional_args or {}
            if not second is None:
                result = op.__func__(self, first, second, **optional_args)
            elif not first is None:
                result = op.__func__(self, first, **optional_args)
            elif isinstance(op, str):
                result = op
            else:
                result = op.__func__(self, **optional_args)

        elif field_type:
            result = self.represent(expression,field_type)
        elif isinstance(expression,(list,tuple)):
            result = [self.represent(item,field_type) for
                      item in expression]
        else:
            result = expression
        return result

    def drop(self, table, mode=''):
        ctable = self.connection[table._tablename]
        ctable.drop()
        self._drop_cleanup(table)
        return

    def truncate(self, table, mode, safe=None):
        ctable = self.connection[table._tablename]
        ctable.remove(None, w=self._get_safe(safe))

    def count(self, query, distinct=None, snapshot=True, return_tuple=False):
        if distinct:
            raise RuntimeError("COUNT DISTINCT not supported")
        if not isinstance(query, Query):
            raise SyntaxError("Not Supported")

        (ctable, _filter) = self._expand_query(query)
        result = ctable.count(filter=_filter)
        if return_tuple:
            return (ctable, _filter, result) 
        return result

    def select(self, query, fields, attributes, snapshot=False):
        mongofields_dict = self.SON()
        new_fields, mongosort_list = [], []
        # try an orderby attribute
        orderby = attributes.get('orderby', False)
        limitby = attributes.get('limitby', False)
        # distinct = attributes.get('distinct', False)
        if 'for_update' in attributes:
            self.db.logger.warning('mongodb does not support for_update')
        for key in set(attributes.keys())-set(('limitby', 'orderby',
                                               'for_update')):
            if attributes[key] is not None:
                self.db.logger.warning(
                    'select attribute not implemented: %s' % key)
        if limitby:
            limitby_skip, limitby_limit = limitby[0], int(limitby[1]) - 1
        else:
            limitby_skip = limitby_limit = 0
        if orderby:
            if isinstance(orderby, (list, tuple)):
                orderby = xorify(orderby)
            # !!!! need to add 'random'
            for f in self.expand(orderby).split(','):
                if f.startswith('-'):
                    mongosort_list.append((f[1:], -1))
                else:
                    mongosort_list.append((f, 1))
        for item in fields:
            if isinstance(item, SQLALL):
                new_fields += item._table
            else:
                new_fields.append(item)
        fields = new_fields
        if isinstance(query, Query):
            tablename = self.get_table(query)
        elif len(fields) != 0:
            if isinstance(fields[0], Expression):
                tablename = self.get_table(fields[0])
            else:
                tablename = fields[0].tablename
        else:
            raise SyntaxError("The table name could not be found in " +
                              "the query nor from the select statement.")

        if query:
            if use_common_filters(query):
                query = self.common_filter(query,[tablename])

        mongoqry_dict = self.expand(query)
        ctable = self.connection[tablename]
        modifiers={'snapshot':snapshot}

        projection = {}
        for field in fields:
            if not isinstance(field, Field) and isinstance(field, Expression):
                for field in fields:
                    if not isinstance(field, Field):
                        p = self.expand_aggregate(field)
                        field.name = str(p)
                        projection.update({field.name:p})
                projection['_id'] = None
                break

        if not len(projection):
            fields = fields or self.db[tablename]
            for field in fields:
                mongofields_dict[field.name] = 1
            mongo_list_dicts = ctable.find(
                mongoqry_dict, mongofields_dict, skip=limitby_skip,
                limit=limitby_limit, sort=mongosort_list, modifiers=modifiers)
            null_rows = []
        else:
            pipeline = []
            if mongoqry_dict != None:
                pipeline.append({ '$match': mongoqry_dict })
            pipeline.append({ '$group': projection })
            mongo_list_dicts = ctable.aggregate(pipeline)
            null_rows = [(None,)]

        rows = []
        # populate row in proper order
        # Here we replace ._id with .id to follow the standard naming
        colnames = []
        newnames = []
        for field in fields:
            if hasattr(field, "tablename"):
                if field.name in ('id', '_id'):
                    # Mongodb reserved uuid key
                    colname = (tablename + "." + 'id', '_id')
                else:
                    colname = (tablename + "." + field.name, field.name)
            else:
                colname = (field.name, field.name)
            colnames.append(colname[1])
            newnames.append(colname[0])

        for record in mongo_list_dicts:
            row = []
            for colname in colnames:
                try:
                    value = record[colname]
                except:
                    value = None
                row.append(value)
            rows.append(row)
        if not rows:
            rows = null_rows

        processor = attributes.get('processor', self.parse)
        result = processor(rows, fields, newnames, blob_decode=True)
        return result

    def insert(self, table, fields, safe=None):
        """Safe determines whether a asynchronous request is done or a
        synchronous action is done
        For safety, we use by default synchronous requests"""

        values = {}
        ctable = self._get_collection(table._tablename, safe)

        for k, v in fields:
            if not k.name in ["id", "safe"]:
                fieldname = k.name
                fieldtype = table[k.name].type
                values[fieldname] = self.represent(v, fieldtype)

        result = ctable.insert_one(values)

        if result.acknowledged:
            Oid = result.inserted_id
            rid = Reference(long(str(Oid), 16))
            (rid._table, rid._record) = (table, None)
            return rid
        else:
            return None

    def update(self, tablename, query, fields, safe=None):
        # return amount of adjusted rows or zero, but no exceptions
        # @ related not finding the result
        if not isinstance(query, Query):
            raise RuntimeError("Not implemented")

        safe = self._get_safe(safe)
        if safe:
            (ctable, _filter) = self._expand_query(query, tablename, safe)
            amount = 0
        else:
            (ctable, _filter, amount) = self.count(
                query, distinct=False, return_tuple=True)
            if amount == 0:
                return amount

        projection = None
        for (field, value) in fields:
            if isinstance(value, Expression):
                # add all fields to projection to pass them through
                projection = dict((f, 1) for f in field.table.fields
                                  if (f not in ("_id", "id")))
                break

        if projection != None:
            for field, value in fields:
                # do not update id fields
                if field.name not in ("_id", "id"):
                    expanded = self.expand_aggregate(value, field.type)
                    if not isinstance(value, Expression):
                        if self.server_version_major >= 2.6:
                            expanded = { '$literal': expanded }

                        # '$literal' not present in server versions < 2.6
                        elif field.type in ['string', 'text', 'password']:
                            expanded = { '$concat': [ expanded ] }
                        elif field.type in ['integer', 'bigint', 'float', 'double']:
                            expanded = { '$add': [ expanded ] }
                        elif field.type == 'boolean':
                            expanded = { '$and': [ expanded ] }
                        elif field.type in ['date', 'time', 'datetime']:
                            expanded = { '$add': [ expanded ] }
                        else:
                            raise RuntimeError("updating with expressions not "
                                + "supported for field type '"
                                + "%s' in MongoDB version < 2.6" % field.type)

                    projection.update({field.name: expanded})
            pipeline = []
            if _filter != None:
                pipeline.append({ '$match': _filter })
            pipeline.append({ '$project': projection })

            try:
                for doc in ctable.aggregate(pipeline):
                    idname = '_id'
                    result = ctable.replace_one({'_id': doc['_id']}, doc)
                    if safe and result.acknowledged:
                        amount += result.matched_count
                return amount
            except Exception as e:
                # TODO Reverse update query to verify that the query suceeded
                raise RuntimeError("uncaught exception when updating rows: %s" % e)

        else:
            # do not update id fields
            update = {'$set': dict((k.name, self.represent(v, k.type)) for
                      k, v in fields if (not k.name in ("_id", "id")))}
            try:
                result = ctable.update_many(filter=_filter, update=update)
                if safe and result.acknowledged:
                    amount = result.matched_count
                return amount
            except Exception as e:
                # TODO Reverse update query to verify that the query suceeded
                raise RuntimeError("uncaught exception when updating rows: %s" % e)

    def delete(self, tablename, query, safe=None):
        if not isinstance(query, Query):
            raise RuntimeError("query type %s is not supported" % type(query))

        (ctable, _filter) = self._expand_query(query, safe)
        deleted = [x['_id'] for x in ctable.find(_filter)]

        # find references to deleted items
        db = self.db
        table = db[tablename]
        cascade = []
        set_null = []
        for field in table._referenced_by:
            if field.type == 'reference '+ tablename:
                if field.ondelete == 'CASCADE':
                    cascade.append(field)
                if field.ondelete == 'SET NULL':
                    set_null.append(field)
        cascade_list = []
        set_null_list = []
        for field in table._referenced_by_list:
            if field.type == 'list:reference '+ tablename:
                if field.ondelete == 'CASCADE':
                    cascade_list.append(field)
                if field.ondelete == 'SET NULL':
                    set_null_list.append(field)

        # perform delete
        result = ctable.delete_many(_filter)
        if result.acknowledged:
            amount = result.deleted_count
        else:
            amount = len(deleted)

        # clean up any references
        if amount and deleted:
            def remove_from_list(field, deleted, safe):
                for delete in deleted:
                    modify = {field.name: delete}
                    dtable = self._get_collection(field.tablename, safe)
                    result = dtable.update_many(
                        filter=modify, update={'$pull': modify})

            # for cascaded items, if the reference is the only item in the list,
            # then remove the entire record, else delete reference from the list 
            for field in cascade_list:
                for delete in deleted:
                    modify = {field.name: [delete]}
                    dtable = self._get_collection(field.tablename, safe)
                    result = dtable.delete_many(filter=modify)
                remove_from_list(field, deleted, safe)
            for field in set_null_list:
                remove_from_list(field, deleted, safe)
            for field in cascade:
                db(field.belongs(deleted)).delete()
            for field in set_null:
                db(field.belongs(deleted)).update(**{field.name:None})

        return amount

    def bulk_insert(self, table, items):
        return [self.insert(table,item) for item in items]

    ## OPERATORS
    def INVERT(self, first):
        #print "in invert first=%s" % first
        return '-%s' % self.expand(first)

    def NOT(self, first):
        op = self.expand(first)
        op_k = list(op)[0]
        op_body = op[op_k]
        r = None
        if type(op_body) is list:
            # apply De Morgan law for and/or
            # not(A and B) -> not(A) or not(B)
            # not(A or B)  -> not(A) and not(B)
            not_op = '$and' if op_k == '$or' else '$or'
            r = {not_op: [self.NOT(first.first), self.NOT(first.second)]}
        else:
            try:
                sub_ops = list(op_body.keys())
                if len(sub_ops) == 1 and sub_ops[0] == '$ne':
                    r = {op_k: op_body['$ne']}
            except:
                r = {op_k: {'$ne': op_body}}
            if r == None:
                r = {op_k: {'$not': op_body}}
        return r

    def AND(self,first,second):
        # pymongo expects: .find({'$and': [{'x':'1'}, {'y':'2'}]})
        return {'$and': [self.expand(first),self.expand(second)]}

    def OR(self,first,second):
        # pymongo expects: .find({'$or': [{'name':'1'}, {'name':'2'}]})
        return {'$or': [self.expand(first),self.expand(second)]}

    def BELONGS(self, first, second):
        if isinstance(second, str):
            # this is broken, the only way second is a string is if it has
            # been converted to SQL.  This no worky.  This might be made to
            # work if _select did not return SQL.
            raise RuntimeError("nested queries not supported")
        items = [self.expand(item, first.type) for item in second]
        return {self.expand(first) : {"$in" : items} }

    def _validate_second (self, first, second):
        if second is None:
            raise RuntimeError("Cannot compare %s with None" % first)

    def EQ(self, first, second=None):
        return {self.expand(first): self.expand(second, first.type)}

    def NE(self, first, second=None):
        return {self.expand(first): {'$ne': self.expand(second, first.type)}}

    def LT(self, first, second=None):
        self._validate_second (first, second)
        return {self.expand(first): {'$lt': self.expand(second, first.type)}}

    def LE(self ,first, second=None):
        self._validate_second (first, second)
        return {self.expand(first): {'$lte': self.expand(second, first.type)}}

    def GT(self, first, second=None):
        self._validate_second (first, second)
        return {self.expand(first): {'$gt': self.expand(second, first.type)}}

    def GE(self, first, second=None):
        self._validate_second (first, second)
        return {self.expand(first): {'$gte': self.expand(second, first.type)}}

    def ADD(self, first, second):
        op_code = '$add'
        for field in [first, second]:
            try:
                if field.type in ['string', 'text', 'password']:
                    op_code = '$concat'
                    break
            except:
                pass
        return {op_code : [self.expand(first), self.expand(second, first.type)]}

    def SUB(self, first, second):
        return {'$subtract': [
            self.expand(first), self.expand(second, first.type)]}

    def MUL(self, first, second):
        return {'$multiply': [
            self.expand(first), self.expand(second, first.type)]}

    def DIV(self, first, second):
        return {'$divide': [
            self.expand(first), self.expand(second, first.type)]}

    def MOD(self, first, second):
        return {'$mod': [
            self.expand(first), self.expand(second, first.type)]}

    _aggregate_map = {
        'SUM': '$sum',
        'MAX': '$max',
        'MIN': '$min',
        'AVG': '$avg',
    }

    def AGGREGATE(self, first, what):
        try:
            return {self._aggregate_map[what]: self.expand_aggregate(first)}
        except:
            raise NotImplementedError("'%s' not implemented" % what)

    def COUNT(self, first, distinct=None):
        if distinct:
            raise NotImplementedError("distinct not implmented for count op")
        return {"$sum": 1}

    def AS(self, first, second):
        raise NotImplementedError(self.error_messages["javascript_needed"])
        return '%s AS %s' % (self.expand(first), second)

    # We could implement an option that simulates a full featured SQL
    # database. But I think the option should be set explicit or
    # implemented as another library.
    def ON(self, first, second):
        raise NotImplementedError("This is not possible in NoSQL" +
                                  " but can be simulated with a wrapper.")
        return '%s ON %s' % (self.expand(first), self.expand(second))

    def COMMA(self, first, second):
        return '%s, %s' % (self.expand(first), self.expand(second))

    #TODO verify full compatibilty with official SQL Like operator
    def _build_like_regex(self, arg,
                          case_sensitive=True,
                          ends_with=False,
                          starts_with=False,
                          whole_string=True,
                          like_wildcards=False):
        import re
        base = self.expand(arg,'string')
        need_regex = (whole_string or not case_sensitive
                      or starts_with or ends_with
                      or like_wildcards and ('_' in base or '%' in base))
        if not need_regex:
            return base
        else:
            expr = re.escape(base)
            if like_wildcards:
                expr = expr.replace('\\%','.*')
                expr = expr.replace('\\_','.').replace('_','.')
            if starts_with:
                pattern = '^%s'
            elif ends_with:
                pattern = '%s$'
            elif whole_string:
                pattern = '^%s$'
            else:
                pattern = '%s'

            regex = { '$regex': pattern % expr }
            if not case_sensitive:
                regex['$options'] = 'i'
            return regex

    def LIKE(self, first, second, case_sensitive=True, escape=None):
        regex = self._build_like_regex(
            second, case_sensitive=case_sensitive, like_wildcards=True)
        return { self.expand(first): regex }

    def ILIKE(self, first, second, escape=None):
        return self.LIKE(first, second, case_sensitive=False, escape=escape)

    def STARTSWITH(self, first, second):
        regex = self._build_like_regex(second, starts_with=True)
        return { self.expand(first): regex }

    def ENDSWITH(self, first, second):
        regex = self._build_like_regex(second, ends_with=True)
        return { self.expand(first): regex }

    #TODO verify full compatibilty with official oracle contains operator
    def CONTAINS(self, first, second, case_sensitive=True):
        ret = None
        if isinstance(second, self.ObjectId):
            val = second

        elif isinstance(first, Field) and first.type == 'list:string':
            if isinstance(second, Field) and second.type == 'string':
                ret = {
                    '$where' :
                    "this.%s.indexOf(this.%s) > -1" % (first.name, second.name)
                }
            else:
                val = self._build_like_regex(
                    second, case_sensitive=case_sensitive, whole_string=True)
        else:
            val = self._build_like_regex(
                second, case_sensitive=case_sensitive, whole_string=False)

        if not ret:
            ret = {self.expand(first): val}

        return ret

class MongoDbAggregateExpander(MongoDBAdapter):
    def __init__ (self):
        self.aggregate = True
        self.expand_aggregate = self.expand

class MongoBlob(Binary):
    MONGO_BLOB_BYTES        = USER_DEFINED_SUBTYPE
    MONGO_BLOB_NON_UTF8_STR = USER_DEFINED_SUBTYPE + 1

    def __new__(cls, value):
        # return None and Binary() unmolested
        if value is None or isinstance(value, Binary):
            return value

        # bytearray is marked as MONGO_BLOB_BYTES
        if isinstance(value, bytearray):
            return Binary.__new__(cls, bytes(value), MongoBlob.MONGO_BLOB_BYTES)

        # return non-strings as Binary(), eg: PY3 bytes()
        if not isinstance(value, basestring):
            return Binary(value)

        # if string is encodable as UTF-8, then return as string
        try:
            value.encode('utf-8')
            return value
        except:
            # string which can not be UTF-8 encoded, eg: pickle strings
            return Binary.__new__(cls, value, MongoBlob.MONGO_BLOB_NON_UTF8_STR)

    def __repr__(self):
        return repr(MongoBlob.decode(self))

    @staticmethod
    def decode(value):
        if isinstance(value, Binary):
            if value.subtype == MongoBlob.MONGO_BLOB_BYTES:
                return bytearray(value)
            if value.subtype == MongoBlob.MONGO_BLOB_NON_UTF8_STR:
                return str(value)
        return value
