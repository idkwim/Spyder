#
# Copyright (c) 2010 Daniel Truemper truemped@googlemail.com
#
# frontier.py 26-Jan-2011
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
#
"""
Generic Frontier implementation.

The :class:`SingleHostFrontier` will only select URIs from the queues by
iterating over all available queues and added into a priority queue.

The priority is calculated based on the timestamp it should be crawled next.

In contrast to the :mod:`spyder.core.sqlitequeues` module, URIs in this module
are represented as :class:`spyder.thrift.gen.ttypes.CrawlUri`.
"""

import time
from datetime import datetime

from Queue import PriorityQueue, Empty, Full
from urlparse import urlparse

from spyder.core.constants import CURI_SITE_USERNAME, CURI_SITE_PASSWORD
from spyder.core.constants import CURI_EXTRACTED_URLS
from spyder.core.dnscache import DnsCache
from spyder.time import serialize_date_time, deserialize_date_time
from spyder.core.log import LoggingMixin
from spyder.core.prioritizer import SimpleTimestampPrioritizer
from spyder.core.sqlitequeues import SQLiteSingleHostUriQueue
from spyder.core.uri_uniq import UniqueUriFilter
from spyder.thrift.gen.ttypes import CrawlUri


# some default port numbers as of /etc/services
PROTOCOLS_DEFAULT_PORT = {
    "http": 80,
    "https": 443,
    "ftp": 21,
    "ftps": 990,
    "sftp": 115,
}


class AbstractBaseFrontier(object, LoggingMixin):
    """
    A base class for implementing frontiers.

    Basically this class provides the different general methods and
    configuration parameters used for frontiers.
    """

    def __init__(self, settings, log_handler, front_end_queues, prioritizer,
        unique_hash='sha1'):
        """
        Initialize the frontier and instantiate the
        :class:`SQLiteSingleHostUriQueue`.

        The default frontier we will use the `sha1` hash function for the
        unique uri filter. For very large crawls you might want to use a
        larger hash function (`sha512`, e.g.)
        """
        LoggingMixin.__init__(self, log_handler, settings.LOG_LEVEL_MASTER)
        # front end queue
        self._prioritizer = prioritizer
        self._front_end_queues = front_end_queues
        # checkpointing
        self._checkpoint_interval = settings.FRONTIER_CHECKPOINTING
        self._uris_added = 0

        # the heap
        self._heap = PriorityQueue(maxsize=settings.FRONTIER_HEAP_SIZE)
        self._heap_min_size = settings.FRONTIER_HEAP_MIN

        # a list of uris currently being crawled.
        self._current_uris = dict()
        # dns cache
        self._dns_cache = DnsCache(settings.FRONTIER_SIZE_DNS_CACHE)
        # unique uri filter
        self._unique_uri = UniqueUriFilter(unique_hash)
        for url in self._front_end_queues.all_uris():
            assert not self._unique_uri.is_known(url, add_if_unknown=True)

        # the sinks
        self._sinks = []

        # timezone
        self._timezone = settings.LOCAL_TIMEZONE
        self._logger.info("frontier::initialized")

    def add_sink(self, sink):
        """
        Add a sink to the frontier. A sink will be responsible for the long
        term storage of the crawled contents.
        """
        self._sinks.append(sink)

    def add_uri(self, curi):
        """
        Add the specified :class:`CrawlUri` to the frontier.

        `next_date` is a datetime object for the next time the uri should be
        crawled.

        Note: time based crawling is never strict, it is generally used as some
        kind of prioritization.
        """
        if self._unique_uri.is_known(curi.url, add_if_unknown=True):
            # we already know this uri
            self._logger.debug("frontier::Trying to update a known uri... " + \
                    "(%s)" % (curi.url,))
            return

        self._logger.info("frontier::Adding '%s' to the frontier" % curi.url)
        self._front_end_queues.add_uri(self._uri_from_curi(curi))
        self._maybe_checkpoint()

    def update_uri(self, curi):
        """
        Update a given uri.
        """
        self._front_end_queues.update_uri(self._uri_from_curi(curi))
        self._maybe_checkpoint()

    def get_next(self):
        """
        Return the next uri scheduled for crawling.
        """
        if self._heap.qsize() < self._heap_min_size:
            self._update_heap()

        try:
            (_next_date, next_uri) = self._heap.get_nowait()
        except Empty:
            # heap is empty, there is nothing to crawl right now!
            # maybe log this in the future
            raise

        return self._crawluri_from_uri(next_uri)

    def close(self):
        """
        Close the underlying frontend queues.
        """
        self._front_end_queues.checkpoint()
        self._front_end_queues.close()

    def _add_to_heap(self, uri, next_date):
        """
        Add an URI to the heap that is ready to be crawled.
        """
        self._heap.put_nowait((next_date, uri))
        (url, _etag, _mod_date, _next_date, _prio) = uri
        self._current_uris[url] = uri
        self._logger.debug("frontier::Adding '%s' to the heap" % url)

    def _reschedule_uri(self, curi):
        """
        Return the `next_crawl_date` for :class:`CrawlUri`s.
        """
        (prio, delta) = self._prioritizer.calculate_priority(curi)
        now = datetime.now(self._timezone)
        return (prio, time.mktime((now + delta).timetuple()))

    def _uri_from_curi(self, curi):
        """
        Create the uri tuple from the :class:`CrawlUri` and calculate the
        priority.

        Overwrite this method in more specific frontiers.
        """
        etag = mod_date = None
        if curi.rep_header:
            if "Etag" in curi.rep_header:
                etag = curi.rep_header["Etag"]
            if "Last-Modified" in curi.rep_header:
                mod_date = time.mktime(deserialize_date_time(
                    curi.rep_header["Last-Modified"]).timetuple())

        (prio, next_crawl_date) = self._reschedule_uri(curi)

        return (curi.url, etag, mod_date, next_crawl_date, prio)

    def _crawluri_from_uri(self, uri):
        """
        Convert an URI tuple to a :class:`CrawlUri`.

        Replace the hostname with the real IP in order to cache DNS queries.
        """
        (url, etag, mod_date, _next_date, prio) = uri

        parsed_url = urlparse(url)

        # dns resolution and caching
        port = parsed_url.port
        if not port:
            port = PROTOCOLS_DEFAULT_PORT[parsed_url.scheme]

        effective_netloc = self._dns_cache["%s:%s" % (parsed_url.hostname,
            port)]

        curi = CrawlUri(url)
        curi.effective_url = url.replace(parsed_url.netloc, "%s:%s" %
                effective_netloc)
        curi.current_priority = prio
        curi.req_header = dict()
        if etag:
            curi.req_header["Etag"] = etag
        if mod_date:
            mod_date_time = datetime.fromtimestamp(mod_date)
            curi.req_header["Last-Modified"] = serialize_date_time(
                    mod_date_time)

        curi.optional_vars = dict()
        if parsed_url.username and parsed_url.password:
            curi.optional_vars[CURI_SITE_USERNAME] = \
                parsed_url.username.encode()
            curi.optional_vars[CURI_SITE_PASSWORD] = \
                parsed_url.password.encode()

        return curi

    def _update_heap(self):
        """
        Abstract method. Implement this in the actual Frontier.

        The implementation should really only add uris to the heap if they can
        be downloaded right away.
        """
        pass

    def _maybe_checkpoint(self, force_checkpoint=False):
        """
        Periodically checkpoint the state db.
        """
        self._uris_added += 1
        if self._uris_added > self._checkpoint_interval or force_checkpoint:
            self._front_end_queues.checkpoint()
            self._uris_added = 0

    def process_successful_crawl(self, curi):
        """
        Called when an URI has been crawled successfully.

        `curi` is a :class:`CrawlUri`
        """
        self.update_uri(curi)

        if curi.optional_vars and CURI_EXTRACTED_URLS in curi.optional_vars:
            for url in curi.optional_vars[CURI_EXTRACTED_URLS].split("\n"):
                if not self._unique_uri.is_known(url):
                    self.add_uri(CrawlUri(url))

        del self._current_uris[curi.url]

        for sink in self._sinks:
            sink.process_successful_crawl(curi)

    def process_not_found(self, curi):
        """
        Called when an URL was not found.

        This could mean, that the URL has been removed from the server. If so,
        do something about it!

        Override this method in the actual frontier implementation.
        """
        del self._current_uris[curi.url]

        for sink in self._sinks:
            sink.process_not_found(curi)

    def process_redirect(self, curi):
        """
        Called when there were too many redirects for an URL, or the site has
        note been updated since the last visit.

        In the latter case, update the internal uri and increase the priority
        level.
        """
        del self._current_uris[curi.url]

        if curi.status_code == 304:
            # the page has not been modified since the last visit! Update it
            # NOTE: prio increasing happens in the prioritizer
            self._front_end_queues.update_uri(self._uri_from_curi(curi))

        for sink in self._sinks:
            sink.process_redirect(curi)

    def process_server_error(self, curi):
        """
        Called when there was some kind of server error.

        Override this method in the actual frontier implementation.
        """
        del self._current_uris[curi.url]

        for sink in self._sinks:
            sink.process_server_error(curi)


class SingleHostFrontier(AbstractBaseFrontier):
    """
    A frontier for crawling a single host.
    """

    def __init__(self, settings, log_handler):
        """
        Initialize the base frontier.
        """
        AbstractBaseFrontier.__init__(self, settings, log_handler,
                SQLiteSingleHostUriQueue(settings.FRONTIER_STATE_FILE),
                SimpleTimestampPrioritizer(settings))

        self._crawl_delay = settings.FRONTIER_CRAWL_DELAY_FACTOR
        self._min_delay = settings.FRONTIER_MIN_DELAY / 1000
        self._next_possible_crawl = time.time()

    def get_next(self):
        """
        Get the next URI.

        Only return the next URI if  we have waited enough.
        """
        if self._heap.qsize() < self._heap_min_size:
            self._update_heap()

        if time.time() >= self._next_possible_crawl:
            (next_date, next_uri) = self._heap.get_nowait()

            now = datetime.now(self._timezone)
            localized_next_date = self._timezone.fromutc(
                    datetime.utcfromtimestamp(next_date))

            if now < localized_next_date:
                raise Empty()

            self._next_possible_crawl = time.time() + self._min_delay
            return self._crawluri_from_uri(next_uri)

        raise Empty()

    def _update_heap(self):
        """
        Update the heap with URIs we should crawl.

        Note: it is possible that the heap is not full after it was updated!
        """
        self._logger.info("frontier::Updating heap")
        for uri in self._front_end_queues.queue_head(n=50):

            (url, _etag, _mod_date, next_date, _prio) = uri

            if url not in self._current_uris:
                try:
                    self._add_to_heap(uri, next_date)
                except Full:
                    # heap is full, return to the caller
                    self._logger.error("singlehostfrontier::Heap is full " + \
                            "during update")
                    return

    def process_successful_crawl(self, curi):
        """
        Add the timebased politeness to this frontier.
        """
        AbstractBaseFrontier.process_successful_crawl(self, curi)
        now = time.time()
        self._next_possible_crawl = now + max(self._crawl_delay *
                curi.req_time, self._min_delay)
        self._logger.debug("singlehostfrontier::Next possible crawl: %s" %
                (self._next_possible_crawl,))
