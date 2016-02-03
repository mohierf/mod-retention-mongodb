#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (C) 2009-2012:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#    Gregory Starck, g.starck@gmail.com
#    Hartmut Goebel, h.goebel@goebel-consult.de
#    Frederic Mohier, frederic.mohier@gmail.com
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

"""
This is a scheduler module to save host/service retention data into a mongodb database
"""

import time
import signal
import base64
import traceback
from multiprocessing import Process
import cPickle

try:
    import pymongo
    from pymongo import MongoClient
    from pymongo.errors import AutoReconnect, ConnectionFailure, InvalidURI, ConfigurationError, DuplicateKeyError
except ImportError:
    logger.error('[Mongodb-Scheduler-Retention] Can not import pymongo and/or MongoClient'
                 'Your pymongo lib is too old. '
                 'Please install it with a 3.x+ version from '
                 'https://pypi.python.org/pypi/pymongo')
    MongoClient = None

try:
    import cPickle as pickle
except ImportError:
    import pickle as pickle

from shinken.basemodule import BaseModule
from shinken.log import logger
from shinken.util import to_bool

properties = {
    'daemons': ['scheduler'],
    'type': 'retention-mongodb',
    'external': False
}


# called by the plugin manager to get a mongodb_retention_scheduler instance
def get_instance(mod_conf):
    logger.info('[Mongodb-Scheduler-Retention] got an instance of MongodbRetentionScheduler module for %s'
                % mod_conf.get_name())
    instance = MongodbRetentionScheduler(mod_conf)
    return instance


class MongodbRetentionSchedulerError(Exception):
    pass


# Main class
class MongodbRetentionScheduler(BaseModule):

    def __init__(self, mod_conf):
        BaseModule.__init__(self, mod_conf)

        self.uri = getattr(mod_conf, 'uri', 'mongodb://localhost')
        logger.info('[Mongodb-Scheduler-Retention] mongo uri: %s', self.uri)

        self.replica_set = getattr(mod_conf, 'replica_set', None)
        if self.replica_set and int(pymongo.version[0]) < 3:
            logger.error('[Mongodb-Scheduler-Retention] Can not initialize module with '
                         'replica_set because your pymongo lib is too old. '
                         'Please install it with a 3.x+ version from '
                         'https://pypi.python.org/pypi/pymongo')
            return None

        self.path = getattr(mod_conf, 'path', None)
        logger.info('[Mongodb-Scheduler-Retention] old file path: %s', self.path)

        self.database = getattr(mod_conf, 'database', 'shinken')
        logger.info('[Mongodb-Scheduler-Retention] database: %s', self.database)

        self.hosts_collection_name = getattr(mod_conf, 'hosts_collection_name', 'retention_hosts')
        logger.info('[Mongodb-Scheduler-Retention] hosts retention collection: %s', self.hosts_collection_name)

        self.services_collection_name = getattr(mod_conf, 'services_collection_name', 'retention_services')
        logger.info('[Mongodb-Scheduler-Retention] services retention collection: %s', self.services_collection_name)

        self.comments_collection_name = getattr(mod_conf, 'comments_collection_name', 'retention_comments')
        logger.info('[Mongodb-Scheduler-Retention] comments retention collection: %s', self.comments_collection_name)

        self.downtimes_collection_name = getattr(mod_conf, 'downtimes_collection_name', 'retention_downtimes')
        logger.info('[Mongodb-Scheduler-Retention] downtimes retention collection: %s', self.downtimes_collection_name)

        self.connection = None
        self.task = None

    def init(self):
        """
        Called by Scheduler to do init work
        """
        return True

    def _open(self):
        """
        Connect to the Mongo DB with configured URI.

        Execute a command to check if connected on master to activate immediate connection to
        the DB because we need to know if DB server is available.
        """
        self._close()

        self.connection = MongoClient(self.uri, connect=False)
        logger.info("[Mongodb-Scheduler-Retention] trying to connect MongoDB: %s", self.uri)
        try:
            result = self.connection.admin.command("ismaster")
            logger.info("[Mongodb-Scheduler-Retention] connected to MongoDB, admin: %s", result)
            logger.info("[Mongodb-Scheduler-Retention] server information: %s", self.connection.server_info())

            self.db = self.connection[self.database]
            logger.info("[Mongodb-Scheduler-Retention] connected to the database: %s (%s)", self.database, self.db)
            self.hosts_collection = self.db[self.hosts_collection_name]
            self.services_collection = self.db[self.services_collection_name]
            self.comments_collection = self.db[self.comments_collection_name]
            self.downtimes_collection = self.db[self.downtimes_collection_name]

            logger.info('[Mongodb-Scheduler-Retention] got collections')
        except ConnectionFailure as e:
            logger.error("[Mongodb-Scheduler-Retention] Server is not available: %s", str(e))
            raise MongodbRetentionSchedulerError
        except (InvalidURI, ConfigurationError):
            logger.error('[Mongodb-Scheduler-Retention] Mongodb connection URI error: %s' % self.uri)
            raise MongodbRetentionSchedulerError
        except Exception as e:
            logger.error("[Mongodb-Scheduler-Retention] Could not open the database", str(e))
            raise MongodbRetentionSchedulerError

    def _close(self):
        """
        Close database connection
        """
        if self.connection:
            self.connection.close()
            self.connection = None
        logger.info('[Mongodb-Scheduler-Retention] database connection closed')

    def _get_element(self, elt, host, service):
        """
        Convert object to dict
        """
        item = {}
        cls = elt.__class__
        item['host'] = host
        item['service'] = service
        for prop in cls.properties:
            if hasattr(elt, prop):
                item[prop] = getattr(elt, prop)

        return item

    def hook_load_retention(self, daemon):
        """
        Called by Scheduler to restore stored retention data
        """
        logger.info('[Mongodb-Scheduler-Retention] retention load starting ...')

        # Now the old flat file way :(
        if self.path:
            logger.info("[Mongodb-Scheduler-Retention] Reading from retention_file %s" % self.path)
            try:
                f = open(self.path, 'rb')
                all_data = cPickle.load(f)
                f.close()
            except (EOFError, ValueError, IOError) as exp:
                logger.warning("[Mongodb-Scheduler-Retention] error reading retention file: %s" % str(exp))
                return False
            except (IndexError, TypeError) as exp:
                logger.warning("[Mongodb-Scheduler-Retention] Sorry, the resource file is not compatible!")
                return False

            # call the scheduler helper function for restoring values
            daemon.restore_retention_data(all_data)

            logger.info("[Mongodb-Scheduler-Retention] Retention objects loaded successfully.")
            return

        try:
            self._open()
        except Exception:
            logger.warn("[Mongodb-Scheduler-Retention] retention load error")
            return

        hosts = {}
        services = {}
        restored_hosts = {}
        restored_services = {}
        try:
            host_cursor = self.hosts_collection.find()
            service_cursor = self.services_collection.find()
            for host in host_cursor:
                value = host.get('value')
                restored_hosts[host.get('_id')] = value
            for service in service_cursor:
                value = service.get('value')
                restored_services[service.get('_id')] = value
            for host in daemon.hosts:
                key = '%s,hostcheck' % (host.host_name)
                if key in restored_hosts:
                    restored_value = restored_hosts[key]
                    value = pickle.loads(base64.b64decode(restored_value))
                    hosts[host.host_name] = value
                    logger.info('[Mongodb-Scheduler-Retention] restored host retention: %s' % (key))
                    if hosts[host.host_name]['downtimes']:
                        for downtime in hosts[host.host_name]['downtimes']:
                            logger.info('[Mongodb-Scheduler-Retention]  - host downtime: %s: %s' % (key, downtime.id))
                    if hosts[host.host_name]['comments']:
                        for comment in hosts[host.host_name]['comments']:
                            logger.info('[Mongodb-Scheduler-Retention]  - host comment: %s: %s' % (key, comment.id))
            for service in daemon.services:
                key = '%s,%s' % (service.host.host_name, service.service_description)
                if key in restored_services:
                    restored_value = restored_services[key]
                    value = pickle.loads(base64.b64decode(restored_value))
                    services[(service.host.host_name,service.service_description)] = value
                    logger.info('[Mongodb-Scheduler-Retention] restored service retention: %s.' % (key))
                    if services[(service.host.host_name,service.service_description)]['downtimes']:
                        for downtime in services[(service.host.host_name,service.service_description)]['downtimes']:
                            logger.info('[Mongodb-Scheduler-Retention]  - host downtime: %s: %s' % (key, downtime.id))
                    if services[(service.host.host_name,service.service_description)]['comments']:
                        for comment in services[(service.host.host_name,service.service_description)]['comments']:
                            logger.info('[Mongodb-Scheduler-Retention]  - host comment: %s: %s' % (key, comment.id))

            retention_data = {'hosts': hosts, 'services': services}
            daemon.restore_retention_data(retention_data)

            logger.info('[Mongodb-Scheduler-Retention] retention load ends')
        except Exception:
            logger.error('[Mongodb-Scheduler-Retention] Retention load error.')
            logger.error('[Mongodb-Scheduler-Retention] %s'
                        % traceback.format_exc())
        finally:
            self._close()

    def hook_save_retention(self, daemon):
        """
        Called by Scheduler to store data

        We must not do anything that will last for a long time. It will delay other operations in
        the Scheduler daemon's main event loop.

        So detach a process that will make the job ...

        If a previous process exists, kill it
        """
        retention = daemon.get_retention_data()
        if self.task and self.task.is_alive():
            logger.warn('[Mongodb-Scheduler-Retention] Previous storing job '
                        'is not yet finished! Make retention interval longer '
                        'in your shinken.cfg configuration.')
            logger.warn('[Mongodb-Scheduler-Retention] Current retention job '
                        'is postponed! ')
            if self.task_count > 2:
                logger.warn('[Mongodb-Scheduler-Retention] retention job has been '
                            'postponed twice. Killing current task to start a new job! ')
                os.kill(self.task.pid, signal.SIGKILL)
            else:
                self.task_count += 1
                return
        self.task = None
        self.task_count = 0

        # Detach a retention job ...
        self.task = Process(target=self._hook_save_retention, args=(retention,))
        self.task.daemon = True
        self.task.start()
        logger.debug('[Mongodb-Scheduler-Retention] New update begins.')

    def _hook_save_retention(self, retention):
        """
        Detached retention task ...
        """
        self.set_proctitle(self.name)
        try:
            self._open()
        except Exception:
            logger.warn("[Mongodb-Scheduler-Retention] retention save error")
            return

        # Hosts / services retention
        now = time.time()
        logger.info('[Mongodb-Scheduler-Retention] update hosts/services retention starting ...')
        hosts = retention['hosts']
        services = retention['services']
        comments = []
        downtimes = []
        try:
            for host in hosts:
                _id = '%s,hostcheck' % host
                logger.info('[Mongodb-Scheduler-Retention] update host retention: %s.' % host)
                host_retention = hosts[host]
                dumped_value = pickle.dumps(host_retention, protocol=pickle.HIGHEST_PROTOCOL)
                value = base64.b64encode(dumped_value)
                self.hosts_collection.remove({'_id': _id})
                retention_data = {'_id': _id,
                                  'value': value,
                                  'timestamp': int(time.time())
                                  }
                self.hosts_collection.insert(retention_data)
                if host_retention['downtimes']:
                    for downtime in host_retention['downtimes']:
                        downtimes.append(('%s,%s' % (_id, downtime.entry_time), self._get_element(downtime, host, 'hostcheck')))
                        logger.info('[Mongodb-Scheduler-Retention]  - host downtime: %s,%s: %s' % (_id, downtime.entry_time, downtime))
                if host_retention['comments']:
                    for comment in host_retention['comments']:
                        comments.append(('%s,%s' % (_id, comment.entry_time), self._get_element(comment, host, 'hostcheck')))
                        logger.info('[Mongodb-Scheduler-Retention]  - host comment: %s,%s: %s' % (_id, comment.entry_time, comment))
            logger.info('[Mongodb-Scheduler-Retention] updated hosts retention.')

            for (host, service) in services:
                _id = '%s,%s' % (host, service)
                logger.info('[Mongodb-Scheduler-Retention] update service retention: %s.' % _id)
                service_retention = services[(host, service)]
                dumped_value = pickle.dumps(service_retention, protocol=pickle.HIGHEST_PROTOCOL)
                value = base64.b64encode(dumped_value)
                self.services_collection.remove({'_id': _id})
                retention_data = {'_id': _id,
                                  'value': value,
                                  'timestamp': int(time.time())
                                  }
                if service_retention['downtimes']:
                    for downtime in service_retention['downtimes']:
                        downtimes.append(('%s,%s' % (_id, downtime.entry_time), self._get_element(downtime, host, service)))
                        logger.info('[Mongodb-Scheduler-Retention]  - service downtime: %s,%s: %s' % (_id, downtime.entry_time, downtime))
                if service_retention['comments']:
                    for comment in service_retention['comments']:
                        comments.append(('%s,%s' % (_id, comment.entry_time), self._get_element(comment, host, service)))
                        logger.info('[Mongodb-Scheduler-Retention]  - service comment: %s,%s: %s' % (_id, comment.entry_time, comment))
                self.services_collection.insert(retention_data)
            logger.info('[Mongodb-Scheduler-Retention] updated services retention.')
        except Exception:
            logger.warn('[Mongodb-Scheduler-Retention] update hosts/services retention error: %s'
                        % traceback.format_exc())

        logger.info("[Mongodb-Scheduler-Retention] update hosts/services retention (%3.4fs)", time.time() - now)

        # Comments / downtimes retention
        now = time.time()
        logger.info('[Mongodb-Scheduler-Retention] update comments/downtimes retention starting ...')
        try:
            for _id, comment in comments:
                filter = { "host": comment['host'], "service": comment['service'], "entry_time": comment['entry_time'] }
                try:
                    result = self.comments_collection.replace_one(filter, comment, upsert=True)
                    if result.upserted_id:
                        logger.info('[Mongodb-Scheduler-Retention] comment inserted: %s / %s.' % (_id, comment))
                except Exception:
                    logger.warn('[Mongodb-Scheduler-Retention] comment update/insert error: %s'
                                % traceback.format_exc())

        except Exception:
            logger.warn('[Mongodb-Scheduler-Retention] comments update error: %s' % traceback.format_exc())

        try:
            for _id, downtime in downtimes:
                filter = { "host": downtime['host'], "service": downtime['service'], "entry_time": downtime['entry_time'] }
                try:
                    result = self.downtimes_collection.replace_one(filter, downtime, upsert=True)
                    if result.upserted_id:
                        logger.info('[Mongodb-Scheduler-Retention] downtime inserted: %s / %s.' % (_id, downtime))
                except Exception:
                    logger.warn('[Mongodb-Scheduler-Retention] downtime update/insert error: %s'
                                % traceback.format_exc())

        except Exception:
            logger.warn('[Mongodb-Scheduler-Retention] comments update error: %s' % traceback.format_exc())

        logger.info("[Mongodb-Scheduler-Retention] update comments/downtimes retention (%3.4fs)", time.time() - now)

        self._close()
