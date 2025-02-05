#
#
#


import logging
import shlex
import re
from threading import Lock

from collections import defaultdict, namedtuple

from lexicon.client import Client as LexiconClient
from lexicon.config import ConfigResolver as LexiconConfigResolver, \
    ConfigSource as LexiconConfigSource

from octodns.provider.base import BaseProvider
from octodns.record import Record

__version__ = "0.1.dev4"


class LexiconProvider(BaseProvider):
    """
    Wrapper to handle LexiconProviders in octodns

    lexicon:
        class: octodns_lexicon.LexiconProvicer

        supports: list of record types to support (A, AAAA, CNAME ...)
                intersects with:
                    LexiconProvider.IMPLEMENTED

        lexicon_config: lexicon config

    Configuration added to the lexicon_config block will be injected as a
    lexicon DictConfigSource. Further config sources read are the env config
    source.

    Example:

        provider:
          gandi:
            class: octodns_lexicon.LexiconProvider
            lexicon_config:
              provider_name: gandi
              domain: blodapels.in
              gandi:
                auth_token: "better kept in environment variable"
                api_protocol: rest

          namecheap:
            class: octodns_provider.lexicon.LexiconProvider
            lexicon_config:
              provider_name: namecheap
              domain: example.com
              namecheap:
                auth_sandbox: True
                auth_username: foobar
                auth_client_ip: 127.0.0.1
                auth_token: "better kept in environment variable"

    """
    IMPLEMENTED = {
        'A', 'AAAA', 'ALIAS', 'CAA', 'CNAME', 'MX', 'NS', 'SRV', 'TXT'}

    SUPPORTS_GEO = False
    SUPPORTS_DYNAMIC = False

    def __init__(self, id, lexicon_config, supports=None, **kwargs):

        self.log = logging.getLogger('LexiconProvider[{}]'.format(id))

        self.SUPPORTS = self.IMPLEMENTED.intersection(
            {s.upper() for s in supports}) if supports else self.IMPLEMENTED

        super(LexiconProvider, self).__init__(id, **kwargs)

        self.log.info('__init__: id=%s, token=***, account=%s', id, kwargs)

        self.remembered_ids = RememberedIds()
        self.lexicon_config = lexicon_config

    def populate(self, zone, target=False, lenient=False):

        loaded_types = defaultdict(lambda: defaultdict(list))
        before = len(zone.records)
        lexicon_client, _ = self._create_client(zone_name=zone.name[:-1])
        exists = False

        lexicon_client.provider.authenticate()
        for lexicon_record in lexicon_client.provider.list_records(
                None, None, None):
            # No way of knowing for sure whether a zone exists or not,
            # But if it has contents, it is safe to assume that it does.
            exists = True
            self.log.debug("provider listed {!s}".format(lexicon_record))

            # harmonize record values here
            if lexicon_record['type'] in ['CNAME', 'MX', 'NS']:
                if not lexicon_record['content'][-1] == '.':
                    domain_part = shlex.split(lexicon_record['content'])[-1]
                    if '.' in domain_part:
                        lexicon_record['content'] += '.'
                    else:
                        lexicon_record['content'] += ".{}".format(zone.name)

                    self.log.info("Harmonizing [%s] -> [%s]",
                                  domain_part, lexicon_record['content'])

            loaded_types[lexicon_record["name"]][lexicon_record["type"]] \
                .append(lexicon_record)

        for record_by_name, data_by_id in loaded_types.items():
            for record_type, lexicon_records in data_by_id.items():
                self.log.debug("Got {!s} from above".format(lexicon_records))

                if record_type in self.SUPPORTS:

                    _data_func = getattr(self,
                                         '_data_for_{}'.format(record_type))

                    data = _data_func(record_type, lexicon_records)

                    self.log.debug('populate: adding record {} records: {!s}'
                                   .format(record_by_name, data))

                    if record_by_name.endswith(zone.name):
                        # This should be handled in the various
                        # Lexicon providers.
                        #  However, there is no harm in doing some extra
                        #  check for it here  - just in case.
                        record_by_name = record_by_name.rstrip('.')

                    if record_by_name.endswith(zone.name[:-1]):
                        record_name = record_by_name[:-(len(zone.name))]
                    else:
                        record_name = record_by_name

                    record = Record.new(zone, record_name, data, source=self)

                    # Some lexicon operations, specifically 'update',
                    # requires the 'identifier' to be used.
                    # Since that information is in the 'id' key, we save it
                    # in a dict from which it can be retrieved when applying
                    #
                    # Furthermore, where octodns saves multi value records as
                    # single record, lexicon has one record for each value.
                    # Therefore, the extra 'content' level is needed here, so
                    # that correct ID for correct record might be retrieved.
                    for lexicon_record in lexicon_records:
                        self.remembered_ids.remember(record,
                                                     lexicon_record['content'],
                                                     lexicon_record['id'])

                    zone.add_record(record, lenient=lenient)

                else:
                    err_str = 'encountered unhandled record type: ' \
                              '"{}" Payload was "{!s}"'.format(record_type,
                                                               lexicon_records)
                    self.log.warning(err_str)

        self.log.info('populate:   found %s records, exists=%s',
                      len(zone.records) - before, before < len(zone.records))

        return exists

    def _create_client(self, zone_name):
        config = LexiconConfigResolver()
        dynamic_config = OnTheFlyLexiconConfigSource(zone_name)

        config.with_config_source(dynamic_config) \
            .with_env().with_dict(self.lexicon_config)

        try:
            return LexiconClient(config), dynamic_config
        except AttributeError as e:
            self.log.error('Unable to parse config {!s}'.format(config))
            raise e

    def _apply(self, plan):
        """Required function of manager.py to actually apply a record change.

            :param plan: Contains the zones and changes to be made
            :type  plan: octodns.provider.base.Plan

            :type return: void
        """
        desired = plan.desired
        changes = plan.changes
        zone_name = plan.existing.name[:-1]
        lexicon_client, dynamic_config = self._create_client(zone_name)

        self.log.debug('_apply: zone=%s, len(changes)=%d', desired.name,
                       len(changes))
        lexicon_client.provider.authenticate()

        for change in changes:
            _rrset_func = getattr(
                self, '_rrset_for_{}'.format(change.record._type))

            # Only way to update TTL is to hope that the provider shall read
            # this one for all operations
            dynamic_config.set_ttl(change.record.ttl)

            old_vars = _rrset_func(change.existing) \
                if change.existing else set()
            new_vars = _rrset_func(change.new) \
                if change.new else set()

            additions = new_vars - old_vars
            deletions = old_vars - new_vars

            additions_iter = iter(sorted(additions))
            deletions_iter = iter(sorted(deletions))

            for i in range(0, min(len(additions), len(deletions))):
                new_record = next(additions_iter)
                old_record = next(deletions_iter)
                identifier = self.remembered_ids.get(change.existing,
                                                     old_record.content)

                if identifier and self.remembered_ids.has_unique_ids(
                        change.existing):

                    self.log.info('client update [id:{}] {!s}'.format(
                        identifier, new_record))

                    if not lexicon_client.provider.update_record(
                            identifier=identifier, **new_record.func_args()):
                        raise RecordUpdateError(new_record, identifier)

                else:
                    self.log.info(
                        'client create_record {!s}'.format(new_record))

                    if not lexicon_client.provider.create_record(
                            **new_record.func_args()):
                        raise RecordCreateError(new_record)

                    self.log.info('client delete_record {!s}'.format(
                        old_record))
                    if not lexicon_client.provider.delete_record(
                            **old_record.func_args()):
                        raise RecordDeleteError(old_record)

            for new_record in additions_iter:
                self.log.info('client create_record {!s}'.format(new_record))
                if not lexicon_client.provider.create_record(
                        **new_record.func_args()):
                    raise RecordCreateError(new_record)

            for old_record in deletions_iter:
                self.log.info('client delete_record {!s}'.format(old_record))
                identifier = self.remembered_ids.get(change.existing,
                                                     old_record.content)

                if not lexicon_client.provider.delete_record(
                        identifier=identifier, **old_record.func_args()):
                    raise RecordDeleteError(old_record)

    def _data_for_multiple(self, _type, lexicon_records):
        return {
            'ttl': lexicon_records[0]['ttl'],
            'type': _type,
            'values': [re.sub(';', '\;', r['content']) for r in lexicon_records]
        }

    def _data_for_CAA(self, _type, lexicon_records):
        values = []
        for record in lexicon_records:
            flags, tag, value = shlex.split(record["content"])
            values.append({
                'flags': flags,
                'tag': tag,
                'value': value
            })
        return {
            'ttl': lexicon_records[0]['ttl'],
            'type': _type,
            'values': values
        }

    def _data_for_CNAME(self, _type, lexicon_records):
        record = lexicon_records[0]
        return {
            'ttl': record['ttl'],
            'type': _type,
            'value': record['content']
        }

    def _data_for_MX(self, _type, lexicon_records):
        values = []
        for record in lexicon_records:
            priority, exchange = shlex.split(record["content"])
            values.append({"priority": priority, "exchange": exchange})

        return {
            'ttl': lexicon_records[0]['ttl'],
            'type': _type,
            'values': values
        }

    def _data_for_SRV(self, _type, lexicon_records):
        values = []
        for record in lexicon_records:
            priority, weight, port, target = shlex.split(record['content'])

            values.append({
                'priority': priority,
                'weight': weight,
                'port': port,
                'target': target
            })
        return {
            'type': _type,
            'ttl': lexicon_records[0]['ttl'],
            'values': values
        }

    _data_for_A = _data_for_multiple

    _data_for_AAAA = _data_for_multiple

    _data_for_ALIAS = _data_for_CNAME

    _data_for_NS = _data_for_multiple

    _data_for_TXT = _data_for_multiple

    def _rrset_for_multiple(self, octodns_record):
        return {LexiconRecord(content=c,
                              ttl=octodns_record.ttl,
                              rtype=octodns_record._type,
                              name=octodns_record.fqdn) for
                c in octodns_record.values}

    def _rrset_for_CAA(self, octodns_record):
        return {LexiconRecord(
            content='{} {} "{}"'.format(c.flags, c.tag, c.value),
            ttl=octodns_record.ttl,
            rtype=octodns_record._type,
            name=octodns_record.fqdn)
            for c in octodns_record.values}

    def _rrset_for_CNAME(self, octodns_record):
        return {LexiconRecord(content=octodns_record.value,
                              ttl=octodns_record.ttl,
                              rtype=octodns_record._type,
                              name=octodns_record.fqdn)}

    def _rrset_for_MX(self, octodns_record):
        return {LexiconRecord(content='{} {}'.format(c.preference, c.exchange),
                              ttl=octodns_record.ttl,
                              rtype=octodns_record._type,
                              name=octodns_record.fqdn)
                for c in octodns_record.values}

    def _rrset_for_SRV(self, octodns_record):
        return {LexiconRecord(
            content='{} {} {} {}'.format(
                c.priority, c.weight, c.port, c.target),
            ttl=octodns_record.ttl,
            rtype=octodns_record._type,
            name=octodns_record.fqdn)
            for c in octodns_record.values}

    _rrset_for_A = _rrset_for_multiple

    _rrset_for_AAAA = _rrset_for_multiple

    _rrset_for_ALIAS = _rrset_for_CNAME

    _rrset_for_NS = _rrset_for_multiple

    _rrset_for_TXT = _rrset_for_multiple


class RememberedIds:

    def __init__(self):
        self.lock = Lock()
        self._id_by_record_and_value = defaultdict(dict)
        self._all_ids_for_record = defaultdict(list)

    def remember(self, record, content, _id):
        with self.lock:
            self._id_by_record_and_value[repr(record)][content] = _id
            self._all_ids_for_record[repr(record)].append(_id)

    def has_unique_ids(self, record):
        # We *want* to use update op when ever possible, because it is
        # safer in the sense that some implementations do perform an update
        # and not a del + add (and what of the time inbetween del and add,
        # and what if it crashes after successfully removing a record,
        # and so forth.
        #
        # However, some providers do not seem to take multi value records
        # into account. Gandi provider for example, derives its id from
        # the records name field, and so to perform an update, it is
        # unclear which of the existing records such an operation would
        # replace if there are more than one value.
        #
        # Therefore, an update operation (when applicable) can only be
        # performed either if all the ids encountered are unique, or else
        # if there are only one value for that record present already, in
        # which case the id is unique simply by being the only one.
        return len(self._all_ids_for_record[repr(record)]) == \
            len(set(self._all_ids_for_record[repr(record)]))

    def get(self, record, content):
        try:
            return self._id_by_record_and_value[repr(record)][content]
        except KeyError:
            return None

    def get_all_ids(self, record):
        return self._all_ids_for_record[repr(record)]


class LexiconRecord(namedtuple('LexiconRecord', 'content ttl rtype name')):

    def to_list_format(self):
        # function called is 'rtype' but list output of record names it 'type'
        return {k if k != 'rtype' else 'type': v for k, v
                in self._asdict().items()}

    def func_args(self):
        # TTL no argument for the functions.
        return {k: getattr(self, k) for k in ['content', 'rtype', 'name']}


class OnTheFlyLexiconConfigSource(LexiconConfigSource):

    def __init__(self, domain, ttl=3600):
        super(OnTheFlyLexiconConfigSource, self).__init__()
        self.ttl = ttl
        self.domain = domain

    def set_ttl(self, ttl):
        self.ttl = ttl

    def resolve(self, config_key):
        if config_key == "lexicon:ttl":
            return self.ttl
        elif config_key == 'lexicon:domain':
            return self.domain
        # These two keys below are not used, because actions are handled in
        # _apply, The config needs to resolve, though, lest the config
        # validation will fail.
        elif config_key == 'lexicon:action':
            return '*'
        elif config_key == 'lexicon:type':
            return '*'
        else:
            return None


class RecordUpdateError(RuntimeError):
    def __init__(self, record, identifier=None):
        msg = "Error handling record: {!s} id:{!s}".format(record, identifier)
        super(RecordUpdateError, self).__init__(msg)


class RecordDeleteError(RecordUpdateError):
    pass


class RecordCreateError(RecordUpdateError):
    pass
