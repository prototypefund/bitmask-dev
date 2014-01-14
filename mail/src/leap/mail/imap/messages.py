# -*- coding: utf-8 -*-
# messages.py
# Copyright (C) 2013 LEAP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
LeapMessage and MessageCollection.
"""
import copy
import logging
import re
import threading
import time
import StringIO

from collections import defaultdict, namedtuple

from twisted.mail import imap4
from twisted.internet import defer
from twisted.python import log
from u1db import errors as u1db_errors
from zope.interface import implements
from zope.proxy import sameProxiedObjects

from leap.common.check import leap_assert, leap_assert_type
from leap.common.decorators import memoized_method
from leap.common.mail import get_email_charset
from leap.mail import walk
from leap.mail.utils import first
from leap.mail.decorators import deferred
from leap.mail.imap.index import IndexedDB
from leap.mail.imap.fields import fields, WithMsgFields
from leap.mail.imap.parser import MailParser, MBoxParser
from leap.mail.messageflow import IMessageConsumer, MessageProducer

logger = logging.getLogger(__name__)

# TODO ------------------------------------------------------------

# [ ] Add linked-from info.
# [ ] Delete incoming mail only after successful write!
# [ ] Remove UID from syncable db. Store only those indexes locally.
# [ ] Send patch to twisted for bug in imap4.py:5717  (content-type can be
#     none? lower-case?)


def lowerdict(_dict):
    """
    Return a dict with the keys in lowercase.

    :param _dict: the dict to convert
    :rtype: dict
    """
    # TODO should properly implement a CaseInsensitive dict.
    # Look into requests code.
    return dict((key.lower(), value)
                for key, value in _dict.items())


CHARSET_PATTERN = r"""charset=([\w-]+)"""
MSGID_PATTERN = r"""<([\w@.]+)>"""

CHARSET_RE = re.compile(CHARSET_PATTERN, re.IGNORECASE)
MSGID_RE = re.compile(MSGID_PATTERN)


class MessagePart(object):
    """
    IMessagePart implementor.
    It takes a subpart message and is able to find
    the inner parts.

    Excusatio non petita: see the interface documentation.
    """

    implements(imap4.IMessagePart)

    def __init__(self, soledad, part_map):
        """
        Initializes the MessagePart.

        :param part_map: a dictionary containing the parts map for this
                         message
        :type part_map: dict
        """
        # TODO
        # It would be good to pass the uid/mailbox also
        # for references while debugging.

        # We have a problem on bulk moves, and is
        # that when the fetch on the new mailbox is done
        # the parts maybe are not complete.
        # So we should be able to fail with empty
        # docs until we solve that. The ideal would be
        # to gather the results of the deferred operations
        # to signal the operation is complete.
        #leap_assert(part_map, "part map dict cannot be null")
        self._soledad = soledad
        self._pmap = part_map

    def getSize(self):
        """
        Return the total size, in octets, of this message part.

        :return: size of the message, in octets
        :rtype: int
        """
        if not self._pmap:
            return 0
        size = self._pmap.get('size', None)
        if not size:
            logger.error("Message part cannot find size in the partmap")
        return size

    def getBodyFile(self):
        """
        Retrieve a file object containing only the body of this message.

        :return: file-like object opened for reading
        :rtype: StringIO
        """
        fd = StringIO.StringIO()
        if self._pmap:
            multi = self._pmap.get('multi')
            if not multi:
                phash = self._pmap.get("phash", None)
            else:
                pmap = self._pmap.get('part_map')
                first_part = pmap.get('1', None)
                if first_part:
                    phash = first_part['phash']

            if not phash:
                logger.warning("Could not find phash for this subpart!")
                payload = str("")
            else:
                payload = self._get_payload_from_document(phash)

        else:
            logger.warning("Message with no part_map!")
            payload = str("")

        if payload:
            content_type = self._get_ctype_from_document(phash)
            charset = first(CHARSET_RE.findall(content_type))
            logger.debug("Got charset from header: %s" % (charset,))
            if not charset:
                charset = self._get_charset(payload)
            try:
                payload = payload.encode(charset)
            except (UnicodeEncodeError, UnicodeDecodeError) as e:
                logger.error("Unicode error {0}".format(e))
                payload = payload.encode(charset, 'replace')

        fd.write(payload)
        fd.seek(0)
        return fd

    # TODO cache the phash retrieval
    def _get_payload_from_document(self, phash):
        """
        Gets the message payload from the content document.

        :param phash: the payload hash to retrieve by.
        :type phash: basestring
        """
        cdocs = self._soledad.get_from_index(
            fields.TYPE_P_HASH_IDX,
            fields.TYPE_CONTENT_VAL, str(phash))

        cdoc = first(cdocs)
        if not cdoc:
            logger.warning(
                "Could not find the content doc "
                "for phash %s" % (phash,))
        payload = cdoc.content.get(fields.RAW_KEY, "")
        return payload

    # TODO cache the pahash retrieval
    def _get_ctype_from_document(self, phash):
        """
        Gets the content-type from the content document.

        :param phash: the payload hash to retrieve by.
        :type phash: basestring
        """
        cdocs = self._soledad.get_from_index(
            fields.TYPE_P_HASH_IDX,
            fields.TYPE_CONTENT_VAL, str(phash))

        cdoc = first(cdocs)
        if not cdoc:
            logger.warning(
                "Could not find the content doc "
                "for phash %s" % (phash,))
        ctype = cdoc.content.get('ctype', "")
        return ctype

    @memoized_method
    def _get_charset(self, stuff):
        # TODO put in a common class with LeapMessage
        """
        Gets (guesses?) the charset of a payload.

        :param stuff: the stuff to guess about.
        :type stuff: basestring
        :returns: charset
        """
        # XXX existential doubt 2. shouldn't we make the scope
        # of the decorator somewhat more persistent?
        # ah! yes! and put memory bounds.
        return get_email_charset(unicode(stuff))

    def getHeaders(self, negate, *names):
        """
        Retrieve a group of message headers.

        :param names: The names of the headers to retrieve or omit.
        :type names: tuple of str

        :param negate: If True, indicates that the headers listed in names
                       should be omitted from the return value, rather
                       than included.
        :type negate: bool

        :return: A mapping of header field names to header field values
        :rtype: dict
        """
        if not self._pmap:
            logger.warning("No pmap in Subpart!")
            return {}
        headers = dict(self._pmap.get("headers", []))

        # twisted imap server expects *some* headers to be lowercase
        # We could use a CaseInsensitiveDict here...
        headers = dict(
            (str(key), str(value)) if key.lower() != "content-type"
            else (str(key.lower()), str(value))
            for (key, value) in headers.items())

        names = map(lambda s: s.upper(), names)
        if negate:
            cond = lambda key: key.upper() not in names
        else:
            cond = lambda key: key.upper() in names

        # unpack and filter original dict by negate-condition
        filter_by_cond = [
            map(str, (key, val)) for
            key, val in headers.items()
            if cond(key)]
        filtered = dict(filter_by_cond)
        return filtered

    def isMultipart(self):
        """
        Return True if this message is multipart.
        """
        if not self._pmap:
            logger.warning("Could not get part map!")
            return False
        multi = self._pmap.get("multi", False)
        return multi

    def getSubPart(self, part):
        """
        Retrieve a MIME submessage

        :type part: C{int}
        :param part: The number of the part to retrieve, indexed from 0.
        :raise IndexError: Raised if the specified part does not exist.
        :raise TypeError: Raised if this message is not multipart.
        :rtype: Any object implementing C{IMessagePart}.
        :return: The specified sub-part.
        """
        if not self.isMultipart():
            raise TypeError
        sub_pmap = self._pmap.get("part_map", {})
        try:
            part_map = sub_pmap[str(part + 1)]
        except KeyError:
            logger.debug("getSubpart for %s: KeyError" % (part,))
            raise IndexError

        # XXX check for validity
        return MessagePart(self._soledad, part_map)


class LeapMessage(fields, MailParser, MBoxParser):
    """
    The main representation of a message.

    It indexes the messages in one mailbox by a combination
    of uid+mailbox name.
    """

    # TODO this has to change.
    # Should index primarily by chash, and keep a local-lonly
    # UID table.

    implements(imap4.IMessage)

    def __init__(self, soledad, uid, mbox):
        """
        Initializes a LeapMessage.

        :param soledad: a Soledad instance
        :type soledad: Soledad
        :param uid: the UID for the message.
        :type uid: int or basestring
        :param mbox: the mbox this message belongs to
        :type mbox: basestring
        """
        MailParser.__init__(self)
        self._soledad = soledad
        self._uid = int(uid)
        self._mbox = self._parse_mailbox_name(mbox)

        self.__chash = None
        self.__bdoc = None

    @property
    def _fdoc(self):
        """
        An accessor to the flags document.
        """
        if all(map(bool, (self._uid, self._mbox))):
            fdoc = self._get_flags_doc()
            if fdoc:
                self.__chash = fdoc.content.get(
                    fields.CONTENT_HASH_KEY, None)
            return fdoc

    @property
    def _chash(self):
        """
        An accessor to the content hash for this message.
        """
        if not self._fdoc:
            return None
        if not self.__chash and self._fdoc:
            self.__chash = self._fdoc.content.get(
                fields.CONTENT_HASH_KEY, None)
        return self.__chash

    @property
    def _hdoc(self):
        """
        An accessor to the headers document.
        """
        return self._get_headers_doc()

    @property
    def _bdoc(self):
        """
        An accessor to the body document.
        """
        if not self._hdoc:
            return None
        if not self.__bdoc:
            self.__bdoc = self._get_body_doc()
        return self.__bdoc

    # IMessage implementation

    def getUID(self):
        """
        Retrieve the unique identifier associated with this message

        :return: uid for this message
        :rtype: int
        """
        return self._uid

    def getFlags(self):
        """
        Retrieve the flags associated with this message

        :return: The flags, represented as strings
        :rtype: tuple
        """
        if self._uid is None:
            return []

        flags = []
        fdoc = self._fdoc
        if fdoc:
            flags = fdoc.content.get(self.FLAGS_KEY, None)
        if flags:
            flags = map(str, flags)
        return tuple(flags)

    # setFlags, addFlags, removeFlags are not in the interface spec
    # but we use them with store command.

    def setFlags(self, flags):
        """
        Sets the flags for this message

        Returns a SoledadDocument that needs to be updated by the caller.

        :param flags: the flags to update in the message.
        :type flags: tuple of str

        :return: a SoledadDocument instance
        :rtype: SoledadDocument
        """
        leap_assert(isinstance(flags, tuple), "flags need to be a tuple")
        log.msg('setting flags: %s' % (self._uid))

        doc = self._fdoc
        if not doc:
            logger.warning(
                "Could not find FDOC for %s:%s while setting flags!" %
                (self._mbox, self._uid))
            return
        doc.content[self.FLAGS_KEY] = flags
        doc.content[self.SEEN_KEY] = self.SEEN_FLAG in flags
        doc.content[self.RECENT_KEY] = self.RECENT_FLAG in flags
        doc.content[self.DEL_KEY] = self.DELETED_FLAG in flags
        self._soledad.put_doc(doc)

    def addFlags(self, flags):
        """
        Adds flags to this message.

        Returns a SoledadDocument that needs to be updated by the caller.

        :param flags: the flags to add to the message.
        :type flags: tuple of str

        :return: a SoledadDocument instance
        :rtype: SoledadDocument
        """
        leap_assert(isinstance(flags, tuple), "flags need to be a tuple")
        oldflags = self.getFlags()
        self.setFlags(tuple(set(flags + oldflags)))

    def removeFlags(self, flags):
        """
        Remove flags from this message.

        Returns a SoledadDocument that needs to be updated by the caller.

        :param flags: the flags to be removed from the message.
        :type flags: tuple of str

        :return: a SoledadDocument instance
        :rtype: SoledadDocument
        """
        leap_assert(isinstance(flags, tuple), "flags need to be a tuple")
        oldflags = self.getFlags()
        self.setFlags(tuple(set(oldflags) - set(flags)))

    def getInternalDate(self):
        """
        Retrieve the date internally associated with this message

        :rtype: C{str}
        :return: An RFC822-formatted date string.
        """
        return str(self._hdoc.content.get(self.DATE_KEY, ''))

    #
    # IMessagePart
    #

    # XXX we should implement this interface too for the subparts
    # so we allow nested parts...

    def getBodyFile(self):
        """
        Retrieve a file object containing only the body of this message.

        :return: file-like object opened for reading
        :rtype: StringIO
        """
        # TODO refactor with getBodyFile in MessagePart
        fd = StringIO.StringIO()
        bdoc = self._bdoc
        if bdoc:
            body = self._bdoc.content.get(self.RAW_KEY, "")
            content_type = bdoc.content.get('content-type', "")
            charset = first(CHARSET_RE.findall(content_type))
            logger.debug("Got charset from header: %s" % (charset,))
            if not charset:
                charset = self._get_charset(body)
            try:
                body = body.decode(charset).encode(charset)
            except (UnicodeEncodeError, UnicodeDecodeError) as e:
                logger.error("Unicode error {0}".format(e))
                body = body.encode(charset, 'replace')

        # We are still returning funky characters from here.
        else:
            logger.warning("No BDOC found for message.")
            body = str("")

        fd.write(body)
        fd.seek(0)
        return fd

    @memoized_method
    def _get_charset(self, stuff):
        """
        Gets (guesses?) the charset of a payload.

        :param stuff: the stuff to guess about.
        :type stuff: basestring
        :returns: charset
        """
        # TODO get from subpart headers
        # XXX existential doubt 2. shouldn't we make the scope
        # of the decorator somewhat more persistent?
        # ah! yes! and put memory bounds.
        return get_email_charset(unicode(stuff))

    def getSize(self):
        """
        Return the total size, in octets, of this message.

        :return: size of the message, in octets
        :rtype: int
        """
        size = None
        if self._fdoc:
            size = self._fdoc.content.get(self.SIZE_KEY, False)
        else:
            logger.warning("No FLAGS doc for %s:%s" % (self._mbox,
                                                       self._uid))
        if not size:
            # XXX fallback, should remove when all migrated.
            size = self.getBodyFile().len
        return size

    def getHeaders(self, negate, *names):
        """
        Retrieve a group of message headers.

        :param names: The names of the headers to retrieve or omit.
        :type names: tuple of str

        :param negate: If True, indicates that the headers listed in names
                       should be omitted from the return value, rather
                       than included.
        :type negate: bool

        :return: A mapping of header field names to header field values
        :rtype: dict
        """
        # TODO split in smaller methods
        headers = self._get_headers()
        if not headers:
            logger.warning("No headers found")
            return {str('content-type'): str('')}

        names = map(lambda s: s.upper(), names)
        if negate:
            cond = lambda key: key.upper() not in names
        else:
            cond = lambda key: key.upper() in names

        if isinstance(headers, list):
            headers = dict(headers)

        # twisted imap server expects *some* headers to be lowercase
        # XXX refactor together with MessagePart method
        headers = dict(
            (str(key), str(value)) if key.lower() != "content-type"
            else (str(key.lower()), str(value))
            for (key, value) in headers.items())

        # unpack and filter original dict by negate-condition
        filter_by_cond = [(key, val) for key, val
                          in headers.items() if cond(key)]

        return dict(filter_by_cond)

    def _get_headers(self):
        """
        Return the headers dict for this message.
        """
        if self._hdoc is not None:
            headers = self._hdoc.content.get(self.HEADERS_KEY, {})
            return headers

        else:
            logger.warning(
                "No HEADERS doc for msg %s:%s" % (
                    self._mbox,
                    self._uid))

    def isMultipart(self):
        """
        Return True if this message is multipart.
        """
        if self._fdoc:
            is_multipart = self._fdoc.content.get(self.MULTIPART_KEY, False)
            return is_multipart
        else:
            logger.warning(
                "No FLAGS doc for msg %s:%s" % (
                    self._mbox,
                    self._uid))

    def getSubPart(self, part):
        """
        Retrieve a MIME submessage

        :type part: C{int}
        :param part: The number of the part to retrieve, indexed from 0.
        :raise IndexError: Raised if the specified part does not exist.
        :raise TypeError: Raised if this message is not multipart.
        :rtype: Any object implementing C{IMessagePart}.
        :return: The specified sub-part.
        """
        if not self.isMultipart():
            raise TypeError
        try:
            pmap_dict = self._get_part_from_parts_map(part + 1)
        except KeyError:
            logger.debug("getSubpart for %s: KeyError" % (part,))
            raise IndexError
        return MessagePart(self._soledad, pmap_dict)

    #
    # accessors
    #

    def _get_part_from_parts_map(self, part):
        """
        Get a part map from the headers doc

        :raises: KeyError if key does not exist
        :rtype: dict
        """
        if not self._hdoc:
            logger.warning("Tried to get part but no HDOC found!")
            return None

        pmap = self._hdoc.content.get(fields.PARTS_MAP_KEY, {})
        return pmap[str(part)]

    def _get_flags_doc(self):
        """
        Return the document that keeps the flags for this
        message.
        """
        flag_docs = self._soledad.get_from_index(
            fields.TYPE_MBOX_UID_IDX,
            fields.TYPE_FLAGS_VAL, self._mbox, str(self._uid))
        return first(flag_docs)

    def _get_headers_doc(self):
        """
        Return the document that keeps the headers for this
        message.
        """
        head_docs = self._soledad.get_from_index(
            fields.TYPE_C_HASH_IDX,
            fields.TYPE_HEADERS_VAL, str(self._chash))
        return first(head_docs)

    def _get_body_doc(self):
        """
        Return the document that keeps the body for this
        message.
        """
        body_phash = self._hdoc.content.get(
            fields.BODY_KEY, None)
        if not body_phash:
            logger.warning("No body phash for this document!")
            return None
        body_docs = self._soledad.get_from_index(
            fields.TYPE_P_HASH_IDX,
            fields.TYPE_CONTENT_VAL, str(body_phash))

        return first(body_docs)

    def __getitem__(self, key):
        """
        Return an item from the content of the flags document,
        for convenience.

        :param key: The key
        :type key: str

        :return: The content value indexed by C{key} or None
        :rtype: str
        """
        return self._fdoc.content.get(key, None)

    # setters

    # XXX to be used in the messagecopier interface?!

    def set_uid(self, uid):
        """
        Set new uid for this message.

        :param uid: the new uid
        :type uid: basestring
        """
        # XXX dangerous! lock?
        self._uid = uid
        d = self._fdoc
        d.content[self.UID_KEY] = uid
        self._soledad.put_doc(d)

    def set_mbox(self, mbox):
        """
        Set new mbox for this message.

        :param mbox: the new mbox
        :type mbox: basestring
        """
        # XXX dangerous! lock?
        self._mbox = mbox
        d = self._fdoc
        d.content[self.MBOX_KEY] = mbox
        self._soledad.put_doc(d)

    # destructor

    @deferred
    def remove(self):
        """
        Remove all docs associated with this message.
        """
        # XXX For the moment we are only removing the flags and headers
        # docs. The rest we leave there polluting your hard disk,
        # until we think about a good way of deorphaning.
        # Maybe a crawler of unreferenced docs.

        # XXX implement elijah's idea of using a PUT document as a
        # token to ensure consistency in the removal.

        uid = self._uid

        fd = self._get_flags_doc()
        #hd = self._get_headers_doc()
        #bd = self._get_body_doc()
        #docs = [fd, hd, bd]

        docs = [fd]

        for d in filter(None, docs):
            try:
                self._soledad.delete_doc(d)
            except Exception as exc:
                logger.error(exc)
        return uid

    def does_exist(self):
        """
        Return True if there is actually a flags message for this
        UID and mbox.
        """
        return self._fdoc is not None


SoledadWriterPayload = namedtuple(
    'SoledadWriterPayload', ['mode', 'payload'])

# TODO we could consider using enum here:
# https://pypi.python.org/pypi/enum

SoledadWriterPayload.CREATE = 1
SoledadWriterPayload.PUT = 2
SoledadWriterPayload.CONTENT_CREATE = 3


class SoledadDocWriter(object):
    """
    This writer will create docs serially in the local soledad database.
    """

    implements(IMessageConsumer)

    def __init__(self, soledad):
        """
        Initialize the writer.

        :param soledad: the soledad instance
        :type soledad: Soledad
        """
        self._soledad = soledad

    def _get_call_for_item(self, item):
        """
        Return the proper call type for a given item.

        :param item: one of the types defined under the
                     attributes of SoledadWriterPayload
        :type item: int
        """
        call = None
        payload = item.payload

        if item.mode == SoledadWriterPayload.CREATE:
            call = self._soledad.create_doc
        elif (item.mode == SoledadWriterPayload.CONTENT_CREATE
              and not self._content_does_exist(payload)):
                call = self._soledad.create_doc
        elif item.mode == SoledadWriterPayload.PUT:
            call = self._soledad.put_doc
        return call

    def _process(self, queue):
        """
        Return the item and the proper call type for the next
        item in the queue if any.

        :param queue: the queue from where we'll pick item.
        :type queue: Queue
        """
        item = queue.get()
        call = self._get_call_for_item(item)
        return item, call

    def consume(self, queue):
        """
        Creates a new document in soledad db.

        :param queue: queue to get item from, with content of the document
                      to be inserted.
        :type queue: Queue
        """
        empty = queue.empty()
        while not empty:
            item, call = self._process(queue)

            if call:
                # XXX should handle the delete case
                # should handle errors
                try:
                    call(item.payload)
                except u1db_errors.RevisionConflict as exc:
                    logger.error("Error: %r" % (exc,))
                    raise exc

            empty = queue.empty()

    """
    Message deduplication.

    We do a query for the content hashes before writing to our beloved
    sqlcipher backend of Soledad. This means, by now, that:

    1. We will not store the same attachment twice, only the hash of it.
    2. We will not store the same message body twice, only the hash of it.

    The first case is useful if you are always receiving the same old memes
    from unwary friends that still have not discovered that 4chan is the
    generator of the internet. The second will save your day if you have
    initiated session with the same account in two different machines. I also
    wonder why would you do that, but let's respect each other choices, like
    with the religious celebrations, and assume that one day we'll be able
    to run Bitmask in completely free phones. Yes, I mean that, the whole GSM
    Stack.
    """

    def _content_does_exist(self, doc):
        """
        Check whether we already have a content document for a payload
        with this hash in our database.

        :param doc: tentative body document
        :type doc: dict
        :returns: True if that happens, False otherwise.
        """
        if not doc:
            return False
        phash = doc[fields.PAYLOAD_HASH_KEY]
        attach_docs = self._soledad.get_from_index(
            fields.TYPE_P_HASH_IDX,
            fields.TYPE_CONTENT_VAL, str(phash))
        if not attach_docs:
            return False

        if len(attach_docs) != 1:
            logger.warning("Found more than one copy of phash %s!"
                           % (phash,))
        logger.debug("Found attachment doc with that hash! Skipping save!")
        return True


class MessageCollection(WithMsgFields, IndexedDB, MailParser, MBoxParser):
    """
    A collection of messages, surprisingly.

    It is tied to a selected mailbox name that is passed to constructor.
    Implements a filter query over the messages contained in a soledad
    database.
    """

    # XXX this should be able to produce a MessageSet methinks
    # could validate these kinds of objects turning them
    # into a template for the class.
    FLAGS_DOC = "FLAGS"
    HEADERS_DOC = "HEADERS"
    CONTENT_DOC = "CONTENT"

    templates = {

        FLAGS_DOC: {
            fields.TYPE_KEY: fields.TYPE_FLAGS_VAL,
            fields.UID_KEY: 1,  # XXX moe to a local table
            fields.MBOX_KEY: fields.INBOX_VAL,
            fields.CONTENT_HASH_KEY: "",

            fields.SEEN_KEY: False,
            fields.RECENT_KEY: True,
            fields.DEL_KEY: False,
            fields.FLAGS_KEY: [],
            fields.MULTIPART_KEY: False,
            fields.SIZE_KEY: 0
        },

        HEADERS_DOC: {
            fields.TYPE_KEY: fields.TYPE_HEADERS_VAL,
            fields.CONTENT_HASH_KEY: "",

            fields.DATE_KEY: "",
            fields.SUBJECT_KEY: "",

            fields.HEADERS_KEY: {},
            fields.PARTS_MAP_KEY: {},
        },

        CONTENT_DOC: {
            fields.TYPE_KEY: fields.TYPE_CONTENT_VAL,
            fields.PAYLOAD_HASH_KEY: "",
            fields.LINKED_FROM_KEY: [],
            fields.CTYPE_KEY: "",  # should index by this too

            # should only get inmutable headers parts
            # (for indexing)
            fields.HEADERS_KEY: {},
            fields.RAW_KEY: "",
            fields.PARTS_MAP_KEY: {},
            fields.HEADERS_KEY: {},
            fields.MULTIPART_KEY: False,
        },

    }

    def __init__(self, mbox=None, soledad=None):
        """
        Constructor for MessageCollection.

        :param mbox: the name of the mailbox. It is the name
                     with which we filter the query over the
                     messages database
        :type mbox: str

        :param soledad: Soledad database
        :type soledad: Soledad instance
        """
        MailParser.__init__(self)
        leap_assert(mbox, "Need a mailbox name to initialize")
        leap_assert(mbox.strip() != "", "mbox cannot be blank space")
        leap_assert(isinstance(mbox, (str, unicode)),
                    "mbox needs to be a string")
        leap_assert(soledad, "Need a soledad instance to initialize")

        # okay, all in order, keep going...
        self.mbox = self._parse_mailbox_name(mbox)
        self._soledad = soledad
        self.initialize_db()

        # I think of someone like nietzsche when reading this

        # this will be the producer that will enqueue the content
        # to be processed serially by the consumer (the writer). We just
        # need to `put` the new material on its plate.

        self.soledad_writer = MessageProducer(
            SoledadDocWriter(soledad),
            period=0.02)

    def _get_empty_doc(self, _type=FLAGS_DOC):
        """
        Returns an empty doc for storing different message parts.
        Defaults to returning a template for a flags document.
        :return: a dict with the template
        :rtype: dict
        """
        if not _type in self.templates.keys():
            raise TypeError("Improper type passed to _get_empty_doc")
        return copy.deepcopy(self.templates[_type])

    def _do_parse(self, raw):
        """
        Parse raw message and return it along with
        relevant information about its outer level.

        :param raw: the raw message
        :type raw: StringIO or basestring
        :return: msg, chash, size, multi
        :rtype: tuple
        """
        msg = self._get_parsed_msg(raw)
        chash = self._get_hash(msg)
        size = len(msg.as_string())
        multi = msg.is_multipart()
        return msg, chash, size, multi

    def _populate_flags(self, flags, uid, chash, size, multi):
        """
        Return a flags doc.

        XXX Missing DOC -----------
        """
        fd = self._get_empty_doc(self.FLAGS_DOC)

        fd[self.MBOX_KEY] = self.mbox
        fd[self.UID_KEY] = uid
        fd[self.CONTENT_HASH_KEY] = chash
        fd[self.SIZE_KEY] = size
        fd[self.MULTIPART_KEY] = multi
        if flags:
            fd[self.FLAGS_KEY] = map(self._stringify, flags)
            fd[self.SEEN_KEY] = self.SEEN_FLAG in flags
            fd[self.DEL_KEY] = self.DELETED_FLAG in flags
            fd[self.RECENT_KEY] = True  # set always by default
        return fd

    def _populate_headr(self, msg, chash, subject, date):
        """
        Return a headers doc.

        XXX Missing DOC -----------
        """
        headers = defaultdict(list)
        for k, v in msg.items():
            headers[k].append(v)

        # "fix" for repeated headers.
        for k, v in headers.items():
            newline = "\n%s: " % (k,)
            headers[k] = newline.join(v)

        lower_headers = lowerdict(headers)
        msgid = first(MSGID_RE.findall(
            lower_headers.get('message-id', '')))

        hd = self._get_empty_doc(self.HEADERS_DOC)
        hd[self.CONTENT_HASH_KEY] = chash
        hd[self.HEADERS_KEY] = headers
        hd[self.MSGID_KEY] = msgid

        if not subject and self.SUBJECT_FIELD in headers:
            hd[self.SUBJECT_KEY] = first(headers[self.SUBJECT_FIELD])
        else:
            hd[self.SUBJECT_KEY] = subject

        if not date and self.DATE_FIELD in headers:
            hd[self.DATE_KEY] = first(headers[self.DATE_FIELD])
        else:
            hd[self.DATE_KEY] = date
        return hd

    def _fdoc_already_exists(self, chash):
        """
        Check whether we can find a flags doc for this mailbox with the
        given content-hash. It enforces that we can only have the same maessage
        listed once for a a given mailbox.

        :param chash: the content-hash to check about.
        :type chash: basestring
        :return: False, if it does not exist, or UID.
        """
        exist = self._get_fdoc_from_chash(chash)
        if exist:
            return exist.content.get(fields.UID_KEY, "unknown-uid")
        else:
            return False

    @deferred
    def add_msg(self, raw, subject=None, flags=None, date=None, uid=1):
        """
        Creates a new message document.
        Here lives the magic of the leap mail. Well, in soledad, really.

        :param raw: the raw message
        :type raw: str

        :param subject: subject of the message.
        :type subject: str

        :param flags: flags
        :type flags: list

        :param date: the received date for the message
        :type date: str

        :param uid: the message uid for this mailbox
        :type uid: int
        """
        # TODO signal that we can delete the original message!-----
        # when all the processing is done.

        # TODO add the linked-from info !

        logger.debug('adding message')
        if flags is None:
            flags = tuple()
        leap_assert_type(flags, tuple)

        # parse
        msg, chash, size, multi = self._do_parse(raw)

        # check for uniqueness.
        if self._fdoc_already_exists(chash):
            logger.warning("We already have that message in this mailbox.")
            # note that this operation will leave holes in the UID sequence,
            # but we're gonna change that all the same for a local-only table.
            # so not touch it by the moment.
            return False

        fd = self._populate_flags(flags, uid, chash, size, multi)
        hd = self._populate_headr(msg, chash, subject, date)

        parts = walk.get_parts(msg)
        body_phash_fun = [walk.get_body_phash_simple,
                          walk.get_body_phash_multi][int(multi)]
        body_phash = body_phash_fun(walk.get_payloads(msg))
        parts_map = walk.walk_msg_tree(parts, body_phash=body_phash)

        # add parts map to header doc
        # (body, multi, part_map)
        for key in parts_map:
            hd[key] = parts_map[key]
        del parts_map

        docs = [fd, hd]
        cdocs = walk.get_raw_docs(msg, parts)

        # Saving
        logger.debug('enqueuing message docs for write')
        ptuple = SoledadWriterPayload

        # first, regular docs: flags and headers
        for doc in docs:
            self.soledad_writer.put(ptuple(
                mode=ptuple.CREATE, payload=doc))

        # and last, but not least, try to create
        # content docs if not already there.
        for cd in cdocs:
            self.soledad_writer.put(ptuple(
                mode=ptuple.CONTENT_CREATE, payload=cd))

    def _remove_cb(self, result):
        return result

    def remove_all_deleted(self):
        """
        Removes all messages flagged as deleted.
        """
        delete_deferl = []
        for msg in self.get_deleted():
            delete_deferl.append(msg.remove())
        d1 = defer.gatherResults(delete_deferl, consumeErrors=True)
        d1.addCallback(self._remove_cb)
        return d1

    def remove(self, msg):
        """
        Remove a given msg.
        :param msg: the message to be removed
        :type msg: LeapMessage
        """
        d = msg.remove()
        d.addCallback(self._remove_cb)
        return d

    # getters: specific queries

    def _get_fdoc_from_chash(self, chash):
        """
        Return a flags document for this mailbox with a given chash.

        :return: A SoledadDocument containing the Flags Document, or None if
                 the query failed.
        :rtype: SoledadDocument or None.
        """
        try:
            query = self._soledad.get_from_index(
                fields.TYPE_MBOX_C_HASH_IDX,
                fields.TYPE_FLAGS_VAL, self.mbox, chash)
            if query:
                if len(query) > 1:
                    logger.warning(
                        "More than one fdoc found for this chash, "
                        "we got a duplicate!!")
                    # XXX we could take action, like trigger a background
                    # process to kill dupes.
                return query.pop()
            else:
                return None
        except Exception as exc:
            logger.exception("Unhandled error %r" % exc)

    def _get_uid_from_msgidCb(self, msgid):
        hdoc = None
        try:
            query = self._soledad.get_from_index(
                fields.TYPE_MSGID_IDX,
                fields.TYPE_HEADERS_VAL, msgid)
            if query:
                if len(query) > 1:
                    logger.warning(
                        "More than one hdoc found for this msgid, "
                        "we got a duplicate!!")
                    # XXX we could take action, like trigger a background
                    # process to kill dupes.
                hdoc = query.pop()
        except Exception as exc:
            logger.exception("Unhandled error %r" % exc)

        if hdoc is None:
            logger.warning("Could not find hdoc for msgid %s"
                           % (msgid,))
            return None
        msg_chash = hdoc.content.get(fields.CONTENT_HASH_KEY)
        fdoc = self._get_fdoc_from_chash(msg_chash)
        if not fdoc:
            logger.warning("Could not find fdoc for msgid %s"
                           % (msgid,))
            return None
        return fdoc.content.get(fields.UID_KEY, None)

    @deferred
    def _get_uid_from_msgid(self, msgid):
        """
        Return a UID for a given message-id.

        It first gets the headers-doc for that msg-id, and
        it found it queries the flags doc for the current mailbox
        for the matching content-hash.

        :return: A UID, or None
        """
        # We need to wait a little bit, cause in some of the cases
        # the query is received right after we've saved the document,
        # and we cannot find it otherwise. This seems to be enough.

        # Doing a sleep since we'll be calling this in a secondary thread,
        # but we'll should be able to collect the results after a
        # reactor.callLater.
        # Maybe we can implement something like NOT_DONE_YET in the web
        # framework, and return from the callback?
        # See: http://jcalderone.livejournal.com/50226.html
        # reactor.callLater(0.3, self._get_uid_from_msgidCb, msgid)
        time.sleep(0.3)
        return self._get_uid_from_msgidCb(msgid)

    # getters: generic for a mailbox

    def get_msg_by_uid(self, uid):
        """
        Retrieves a LeapMessage by UID.

        :param uid: the message uid to query by
        :type uid: int

        :return: A LeapMessage instance matching the query,
                 or None if not found.
        :rtype: LeapMessage
        """
        msg = LeapMessage(self._soledad, uid, self.mbox)
        if not msg.does_exist():
            return None
        return msg

    def get_all_docs(self, _type=fields.TYPE_FLAGS_VAL):
        """
        Get all documents for the selected mailbox of the
        passed type. By default, it returns the flag docs.

        If you want acess to the content, use __iter__ instead

        :return: a list of u1db documents
        :rtype: list of SoledadDocument
        """
        if _type not in fields.__dict__.values():
            raise TypeError("Wrong type passed to get_all_docs")

        if sameProxiedObjects(self._soledad, None):
            logger.warning('Tried to get messages but soledad is None!')
            return []

        all_docs = [doc for doc in self._soledad.get_from_index(
            fields.TYPE_MBOX_IDX,
            _type, self.mbox)]

        # inneficient, but first let's grok it and then
        # let's worry about efficiency.
        # XXX FIXINDEX -- should implement order by in soledad
        return sorted(all_docs, key=lambda item: item.content['uid'])

    def all_msg_iter(self):
        """
        Return an iterator trhough the UIDs of all messages, sorted in
        ascending order.
        """
        all_uids = (doc.content[self.UID_KEY] for doc in
                    self._soledad.get_from_index(
                        fields.TYPE_MBOX_IDX,
                        fields.TYPE_FLAGS_VAL, self.mbox))
        return (u for u in sorted(all_uids))

    def count(self):
        """
        Return the count of messages for this mailbox.

        :rtype: int
        """
        count = self._soledad.get_count_from_index(
            fields.TYPE_MBOX_IDX,
            fields.TYPE_FLAGS_VAL, self.mbox)
        return count

    # unseen messages

    def unseen_iter(self):
        """
        Get an iterator for the message UIDs with no `seen` flag
        for this mailbox.

        :return: iterator through unseen message doc UIDs
        :rtype: iterable
        """
        return (doc.content[self.UID_KEY] for doc in
                self._soledad.get_from_index(
                    fields.TYPE_MBOX_SEEN_IDX,
                    fields.TYPE_FLAGS_VAL, self.mbox, '0'))

    def count_unseen(self):
        """
        Count all messages with the `Unseen` flag.

        :returns: count
        :rtype: int
        """
        count = self._soledad.get_count_from_index(
            fields.TYPE_MBOX_SEEN_IDX,
            fields.TYPE_FLAGS_VAL, self.mbox, '0')
        return count

    def get_unseen(self):
        """
        Get all messages with the `Unseen` flag

        :returns: a list of LeapMessages
        :rtype: list
        """
        return [LeapMessage(self._soledad, docid, self.mbox)
                for docid in self.unseen_iter()]

    # recent messages

    def recent_iter(self):
        """
        Get an iterator for the message UIDs with `recent` flag.

        :return: iterator through recent message docs
        :rtype: iterable
        """
        return (doc.content[self.UID_KEY] for doc in
                self._soledad.get_from_index(
                    fields.TYPE_MBOX_RECT_IDX,
                    fields.TYPE_FLAGS_VAL, self.mbox, '1'))

    def get_recent(self):
        """
        Get all messages with the `Recent` flag.

        :returns: a list of LeapMessages
        :rtype: list
        """
        return [LeapMessage(self._soledad, docid, self.mbox)
                for docid in self.recent_iter()]

    def count_recent(self):
        """
        Count all messages with the `Recent` flag.

        :returns: count
        :rtype: int
        """
        count = self._soledad.get_count_from_index(
            fields.TYPE_MBOX_RECT_IDX,
            fields.TYPE_FLAGS_VAL, self.mbox, '1')
        return count

    # deleted messages

    def deleted_iter(self):
        """
        Get an iterator for the message UIDs with `deleted` flag.

        :return: iterator through deleted message docs
        :rtype: iterable
        """
        return (doc.content[self.UID_KEY] for doc in
                self._soledad.get_from_index(
                    fields.TYPE_MBOX_DEL_IDX,
                    fields.TYPE_FLAGS_VAL, self.mbox, '1'))

    def get_deleted(self):
        """
        Get all messages with the `Deleted` flag.

        :returns: a generator of LeapMessages
        :rtype: generator
        """
        return (LeapMessage(self._soledad, docid, self.mbox)
                for docid in self.deleted_iter())

    def __len__(self):
        """
        Returns the number of messages on this mailbox.

        :rtype: int
        """
        return self.count()

    def __iter__(self):
        """
        Returns an iterator over all messages.

        :returns: iterator of dicts with content for all messages.
        :rtype: iterable
        """
        return (LeapMessage(self._soledad, docuid, self.mbox)
                for docuid in self.all_msg_iter())

    def __repr__(self):
        """
        Representation string for this object.
        """
        return u"<MessageCollection: mbox '%s' (%s)>" % (
            self.mbox, self.count())

    # XXX should implement __eq__ also !!!
    # --- use the content hash for that, will be used for dedup.
