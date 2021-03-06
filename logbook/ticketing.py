# -*- coding: utf-8 -*-
"""
    logbook.ticketing
    ~~~~~~~~~~~~~~~~~

    Implements long handlers that write to remote data stores and assign
    each logging message a ticket id.

    :copyright: (c) 2010 by Armin Ronacher, Georg Brandl.
    :license: BSD, see LICENSE for more details.
"""
import hashlib
from time import time
from logbook.base import NOTSET, cached_property, _level_name_property, \
     LogRecord
from logbook.handlers import Handler

try:
    import simplejson as json
except ImportError:
    import json


class Ticket(object):
    """Represents a ticket from the database."""

    level_name = _level_name_property()

    def __init__(self, db, row):
        self.db = db
        self.__dict__.update(row)

    @cached_property
    def last_occurrence(self):
        """The last occurrence."""
        rv = self.get_occurrences(limit=1)
        if rv:
            return rv[0]

    def get_occurrences(self, order_by='-time', limit=50, offset=0):
        """Returns the occurrences for this ticket."""
        return self.db.get_occurrences(self.ticket_id, order_by, limit, offset)

    def solve(self):
        """Marks this ticket as solved."""
        self.db.solve_ticket(self.ticket_id)
        self.solved = True

    def delete(self):
        """Deletes the ticket from the database."""
        self.db.delete_ticket(self.ticket_id)

    def __eq__(self, other):
        equal = True
        for key in self.__dict__.keys():
            if getattr(self, key) != getattr(other, key):
                equal = False
                break
        return equal


class Occurrence(LogRecord):
    """Represents an occurrence of a ticket."""

    def __init__(self, db, row):
        self.update_from_dict(json.loads(row['data']))
        self.db = db
        self.time = row['time']
        self.ticket_id = row['ticket_id']
        self.occurrence_id = row['occurrence_id']


class DatabaseBackend(object):
    """Provides an abstract interface to various databases."""

    def __init__(self, **options):
        self.options = options
        self.setup_backend()

    def setup_backend(self):
        """Setup the database backend."""

    def record_ticket(self, record, data, hash, app_id):
        """Records a log record as ticket."""

    def count_tickets(self):
        """Returns the number of tickets."""

    def get_tickets(self, order_by='-last_occurrence_time', limit=50, offset=0):
        """Selects tickets from the database."""

    def solve_ticket(self, ticket_id):
        """Marks a ticket as solved."""

    def delete_ticket(self, ticket_id):
        """Deletes a ticket from the database."""

    def get_ticket(self, ticket_id):
        """Return a single ticket with all occurrences."""

    def get_occurrences(self, ticket, order_by='-time', limit=50, offset=0):
        """Selects occurrences from the database for a ticket."""


class SQLAlchemyBackend(DatabaseBackend):
    """Provides access to the database the :class:`TicketingDatabaseHandler`
    is using.
    """

    def setup_backend(self):
        from sqlalchemy import create_engine, MetaData
        engine_or_uri = self.options.pop('uri', None)
        metadata = self.options.pop('metadata', None)
        table_prefix = self.options.pop('table_prefix', 'logbook_')

        if hasattr(engine_or_uri, 'execute'):
            self.engine = engine_or_uri
        else:
            self.engine = create_engine(engine_or_uri, convert_unicode=True)
        if metadata is None:
            metadata = MetaData()
        self.table_prefix = table_prefix
        self.metadata = metadata
        self.create_tables()
        if self.options.get('autocreate_tables', True):
            self.metadata.create_all(bind=self.engine)

    def create_tables(self):
        """Creates the tables required for the handler on the class and
        metadata.
        """
        import sqlalchemy as db
        def table(name, *args, **kwargs):
            return db.Table(self.table_prefix + name, self.metadata,
                            *args, **kwargs)
        self.tickets = table('tickets',
            db.Column('ticket_id', db.Integer, primary_key=True),
            db.Column('record_hash', db.String(40), unique=True),
            db.Column('level', db.Integer),
            db.Column('logger_name', db.String(120)),
            db.Column('location', db.String(512)),
            db.Column('module', db.String(256)),
            db.Column('last_occurrence_time', db.DateTime),
            db.Column('occurrence_count', db.Integer),
            db.Column('solved', db.Boolean),
            db.Column('app_id', db.String(80))
        )
        self.occurrences = table('occurrences',
            db.Column('occurrence_id', db.Integer, primary_key=True),
            db.Column('ticket_id', db.Integer,
                      db.ForeignKey(self.table_prefix + 'tickets.ticket_id')),
            db.Column('time', db.DateTime),
            db.Column('data', db.Text),
            db.Column('app_id', db.String(80))
        )

    def _order(self, q, table, order_by):
        if order_by[0] == '-':
            return q.order_by(table.c[order_by[1:]].desc())
        return q.order_by(table.c[order_by])

    def record_ticket(self, record, data, hash, app_id):
        """Records a log record as ticket."""
        cnx = self.engine.connect()
        trans = cnx.begin()
        try:
            q = self.tickets.select(self.tickets.c.record_hash == hash)
            row = cnx.execute(q).fetchone()
            if row is None:
                row = cnx.execute(self.tickets.insert().values(
                    record_hash=hash,
                    level=record.level,
                    logger_name=record.logger_name or u'',
                    location=u'%s:%d' % (record.filename, record.lineno),
                    module=record.module or u'<unknown>',
                    occurrence_count=0,
                    solved=False,
                    app_id=app_id
                ))
                ticket_id = row.inserted_primary_key[0]
            else:
                ticket_id = row['ticket_id']
            cnx.execute(self.occurrences.insert()
                .values(ticket_id=ticket_id,
                        time=record.time,
                        app_id=app_id,
                        data=json.dumps(data)))
            cnx.execute(self.tickets.update()
                .where(self.tickets.c.ticket_id == ticket_id)
                .values(occurrence_count=self.tickets.c.occurrence_count + 1,
                        last_occurrence_time=record.time,
                        solved=False))
            trans.commit()
        except Exception:
            trans.rollback()
            raise
        cnx.close()

    def count_tickets(self):
        """Returns the number of tickets."""
        return self.engine.execute(self.tickets.count()).fetchone()[0]

    def get_tickets(self, order_by='-last_occurrence_time', limit=50, offset=0):
        """Selects tickets from the database."""
        return [Ticket(self, row) for row in self.engine.execute(
            self._order(self.tickets.select(), self.tickets, order_by)
            .limit(limit).offset(offset)).fetchall()]

    def solve_ticket(self, ticket_id):
        """Marks a ticket as solved."""
        self.engine.execute(self.tickets.update()
            .where(self.tickets.c.ticket_id == ticket_id)
            .values(solved=True))

    def delete_ticket(self, ticket_id):
        """Deletes a ticket from the database."""
        self.engine.execute(self.occurrences.delete()
            .where(self.occurrences.c.ticket_id == ticket_id))
        self.engine.execute(self.tickets.delete()
            .where(self.tickets.c.ticket_id == ticket_id))

    def get_ticket(self, ticket_id):
        """Return a single ticket with all occurrences."""
        row = self.engine.execute(self.tickets.select().where(
            self.tickets.c.ticket_id == ticket_id)).fetchone()
        if row is not None:
            return Ticket(self, row)

    def get_occurrences(self, ticket, order_by='-time', limit=50, offset=0):
        """Selects occurrences from the database for a ticket."""
        return [Occurrence(self, row) for row in
                self.engine.execute(self._order(self.occurrences.select()
                    .where(self.occurrences.c.ticket_id == ticket),
                    self.occurrences, order_by)
                .limit(limit).offset(offset)).fetchall()]


class MongoDBBackend(DatabaseBackend):
    """Provides access to the database the :class:`TicketingDatabaseHandler`
    is using.
    """

    class _FixedTicketClass(Ticket):
        @property
        def ticket_id(self):
            return self._id

    class _FixedOccurrenceClass(Occurrence):
        def __init__(self, db, row):
            self.update_from_dict(json.loads(row['data']))
            self.db = db
            self.time = row['time']
            self.ticket_id = row['ticket_id']
            self.occurrence_id = row['_id']

    #TODO: Update connection setup once PYTHON-160 is solved.
    def setup_backend(self):
        from pymongo.connection import Connection, _parse_uri
        from pymongo.errors import AutoReconnect

        _connection = None
        uri = self.options.pop('uri', u'')
        _connection_attempts = 0

        hosts, database, user, password = _parse_uri(uri, Connection.PORT)

        # Handle auto reconnect signals properly
        while _connection_attempts < 5:
            try:
                if _connection is None:
                    _connection = Connection(uri)
                database = _connection[database]
                break
            except AutoReconnect:
                _connection_attempts += 1
                time.sleep(0.1)

        self.database = database

    def _order(self, q, order_by):
        from pymongo import ASCENDING, DESCENDING
        col = '%s' % (order_by[1:] if order_by[0] == '-' else order_by)
        if order_by[0] == '-':
            return q.sort(col, DESCENDING)
        return q.sort(col, ASCENDING)

    def _oid(self, ticket_id):
        from pymongo.objectid import ObjectId
        return ObjectId(ticket_id)

    def record_ticket(self, record, data, hash, app_id):
        """Records a log record as ticket."""
        db = self.database
        try:
            ticket = db.tickets.find_one({'record_hash': hash})
            if not ticket:
                doc = {'record_hash': hash,
                       'level': record.level,
                       'logger_name': record.logger_name or u'',
                       'location': u'%s:%d' % (record.filename, record.lineno),
                       'module': record.module or u'<unknown>',
                       'orrucrence_count': 0,
                       'solved': False,
                       'app_id': app_id,
                       'occurrences': []}
                ticket_id = db.tickets.insert(doc)
            else:
                ticket_id = ticket['_id']

            db.tickets.update({'_id': ticket_id}, {
                '$push': {'occurrences': {
                    'app_id': app_id,
                    'data': json.dumps(data),
                    'time': record.time}},
                '$inc': {'occurrence_count': 1},
                '$set': {'last_occurrence_time': record.time,
                         'solved': False}
            })
            # We store occurrences in a seperate collection so that
            # we can make it a capped collection optionally.
            db.occurrences.insert({
                'ticket_id': self._oid(ticket_id),
                'app_id': app_id,
                'time': record.time,
                'data': json.dumps(data),
            })
        except Exception:
            raise

    def count_tickets(self):
        """Returns the number of tickets."""
        return self.database.tickets.find().count()

    def get_tickets(self, order_by='-last_occurrence_time', limit=50, offset=0):
        """Selects tickets from the database."""
        query = self._order(self.database.tickets.find(), order_by) \
                    .limit(limit).skip(offset)
        return [self._FixedTicketClass(self, obj) for obj in query]


    def solve_ticket(self, ticket_id):
        """Marks a ticket as solved."""
        self.database.tickets.update({'_id': self._oid(ticket_id)},
                                     {'solved': True})

    def delete_ticket(self, ticket_id):
        """Deletes a ticket from the database."""
        self.database.occurrences.remove({'ticket_id': self._oid(ticket_id)})
        self.database.tickets.remove({'_id': self._oid(ticket_id)})

    def get_ticket(self, ticket_id):
        """Return a single ticket with all occurrences."""
        ticket = self.database.tickets.find_one({'_id': self._oid(ticket_id)})
        if ticket:
            return Ticket(self, ticket)

    def get_occurrences(self, ticket, order_by='-time', limit=50, offset=0):
        """Selects occurrences from the database for a ticket."""
        collection = self.database.occurrences
        occurrences = self._order(collection.find(
            {'ticket_id': self._oid(ticket)}
        ), order_by).limit(limit).skip(offset)
        return [self._FixedOccurrenceClass(self, obj) for obj in occurrences]


class TicketingDatabaseHandler(Handler):
    """A handler that writes log records into a remote database.  This
    database can be connected to from different dispatchers which makes
    this a nice setup for web applications::

        from logbook.ticketing import TicketingDatabaseHandler
        handler = TicketingDatabaseHandler('sqlite:////tmp/myapp-logs.db')
    """

    _default_backend = SQLAlchemyBackend

    def __init__(self, uri, app_id='generic', level=NOTSET,
                 filter=None, bubble=False, hash_salt=None, backend=None,
                 **db_options):
        Handler.__init__(self, level, filter, bubble)
        if backend is None:
            backend = self._default_backend
        db_options['uri'] = uri
        self.set_backend(backend, **db_options)
        self.app_id = app_id
        self.hash_salt = hash_salt or app_id.encode('utf-8')

    def set_backend(self, cls, **options):
        self.db = cls(**options)

    def hash_record(self, record):
        """Returns the unique hash of a record."""
        hash = hashlib.sha1()
        hash.update('%d\x00' % record.level)
        hash.update((record.logger_name or u'').encode('utf-8') + '\x00')
        hash.update(record.filename.encode('utf-8') + '\x00')
        hash.update(str(record.lineno))
        if record.module:
            hash.update('\x00' + record.module)
        if self.hash_salt is not None:
            hash.update('\x00' + self.hash_salt)
        return hash.hexdigest()

    def process_record(self, record, hash):
        """Subclasses can override this to tamper with the data dict that
        is sent to the database as JSON.
        """
        return record.to_dict(json_safe=True)

    def emit(self, record):
        """Emits a single record and writes it to the database."""
        hash = self.hash_record(record)
        data = self.process_record(record, hash)
        self.db.record_ticket(record, data, hash, self.app_id)
