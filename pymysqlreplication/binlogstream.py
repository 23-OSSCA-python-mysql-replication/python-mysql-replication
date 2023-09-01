# -*- coding: utf-8 -*-

import struct
from distutils.version import LooseVersion

import pymysql
from pymysql.constants.COMMAND import COM_BINLOG_DUMP, COM_REGISTER_SLAVE
from pymysql.cursors import Cursor, DictCursor
from pymysql.connections import Connection, MysqlPacket

from .constants.BINLOG import TABLE_MAP_EVENT, ROTATE_EVENT, FORMAT_DESCRIPTION_EVENT
from .event import (
    QueryEvent, RotateEvent, FormatDescriptionEvent,
    XidEvent, GtidEvent, StopEvent, XAPrepareEvent,
    BeginLoadQueryEvent, ExecuteLoadQueryEvent,
    HeartbeatLogEvent, NotImplementedEvent, MariadbGtidEvent,
    MariadbAnnotateRowsEvent, RandEvent, MariadbStartEncryptionEvent, RowsQueryLogEvent,
    MariadbGtidListEvent, MariadbBinLogCheckPointEvent)
from .exceptions import BinLogNotEnabled
from .gtid import GtidSet
from .packet import BinLogPacketWrapper
from .row_event import (
    UpdateRowsEvent, WriteRowsEvent, DeleteRowsEvent, TableMapEvent)
from typing import ByteString, Union, Optional, List, Tuple, Dict, Any, Iterator, FrozenSet, Type

try:
    from pymysql.constants.COMMAND import COM_BINLOG_DUMP_GTID
except ImportError:
    # Handle old pymysql versions
    # See: https://github.com/PyMySQL/PyMySQL/pull/261
    COM_BINLOG_DUMP_GTID = 0x1e

# 2013 Connection Lost
# 2006 MySQL server has gone away
MYSQL_EXPECTED_ERROR_CODES = [2013, 2006]


class ReportSlave(object):
    """
    Represent the values that you may report
    when connecting as a slave to a master. SHOW SLAVE HOSTS related.
    """

    def __init__(self, value: Union[str, Tuple[str, str, str, int], Dict[str, Union[str, int]]]) -> None:
        """
        Attributes:
            value: string, tuple or dict
                   if string, then it will be used hostname
                   if tuple it will be used as (hostname, user, password, port)
                   if dict, keys 'hostname', 'username', 'password', 'port' will be used.
        """
        self.hostname: str = ''
        self.username: str = ''
        self.password: str = ''
        self.port: int = 0

        if isinstance(value, (tuple, list)):
            try:
                self.hostname: str = value[0]
                self.username: str = value[1]
                self.password: str = value[2]
                self.port: int = int(value[3])
            except IndexError:
                pass
        elif isinstance(value, dict):
            for key in ['hostname', 'username', 'password', 'port']:
                try:
                    setattr(self, key, value[key])
                except KeyError:
                    pass
        else:
            self.hostname: Union[str, tuple] = value

    def __repr__(self) -> str:
        return '<ReportSlave hostname=%s username=%s password=%s port=%d>' % \
            (self.hostname, self.username, self.password, self.port)

    def encoded(self, server_id: int, master_id: int = 0) -> ByteString:
        """
        :ivar server_id: int - the slave server-id
        :ivar master_id: int - usually 0. Appears as "master id" in SHOW SLAVE HOSTS on the master.
                               Unknown what else it impacts.
        """

        # 1              [15] COM_REGISTER_SLAVE
        # 4              server-id
        # 1              slaves hostname length
        # string[$len]   slaves hostname
        # 1              slaves user len
        # string[$len]   slaves user
        # 1              slaves password len
        # string[$len]   slaves password
        # 2              slaves mysql-port
        # 4              replication rank
        # 4              master-id

        lhostname: int = len(self.hostname.encode())
        lusername: int = len(self.username.encode())
        lpassword: int = len(self.password.encode())

        packet_len: int = (1 +  # command
                           4 +  # server-id
                           1 +  # hostname length
                           lhostname +
                           1 +  # username length
                           lusername +
                           1 +  # password length
                           lpassword +
                           2 +  # slave mysql port
                           4 +  # replication rank
                           4)  # master-id

        MAX_STRING_LEN: int = 257  # one byte for length + 256 chars

        return (struct.pack('<i', packet_len) +
                bytes(bytearray([COM_REGISTER_SLAVE])) +
                struct.pack('<L', server_id) +
                struct.pack('<%dp' % min(MAX_STRING_LEN, lhostname + 1),
                            self.hostname.encode()) +
                struct.pack('<%dp' % min(MAX_STRING_LEN, lusername + 1),
                            self.username.encode()) +
                struct.pack('<%dp' % min(MAX_STRING_LEN, lpassword + 1),
                            self.password.encode()) +
                struct.pack('<H', self.port) +
                struct.pack('<l', 0) +
                struct.pack('<l', master_id))


class BinLogStreamReader(object):
    """
    Connect to replication stream and read event
    """
    report_slave: Optional[Union[str, Tuple[str, str, str, int]]] = None

    def __init__(self, connection_settings: Dict, server_id: int,
                 ctl_connection_settings: Optional[Dict] = None, resume_stream: bool = False,
                 blocking: bool = False, only_events: Optional[List[str]] = None, log_file: Optional[str] = None,
                 log_pos: Optional[int] = None, end_log_pos: Optional[int] = None,
                 filter_non_implemented_events: bool = True,
                 ignored_events: Optional[List[str]] = None, auto_position: Optional[str] = None,
                 only_tables: Optional[List[str]] = None, ignored_tables: Optional[List[str]] = None,
                 only_schemas: Optional[List[str]] = None, ignored_schemas: Optional[List[str]] = None,
                 freeze_schema: bool = False, skip_to_timestamp: Optional[float] = None,
                 report_slave: Optional[Union[str, Tuple[str, str, str, int]]] = None, slave_uuid: Optional[str] = None,
                 pymysql_wrapper: Optional[Connection] = None,
                 fail_on_table_metadata_unavailable: bool = False,
                 slave_heartbeat: Optional[float] = None,
                 is_mariadb: bool = False,
                 annotate_rows_event: bool = False,
                 ignore_decode_errors: bool = False) -> None:
        """
        Attributes:
            ctl_connection_settings[Dict]: Connection settings for cluster holding
                                     schema information
            resume_stream[bool]: Start for event from position or the latest event of
                           binlog or from older available event
            blocking[bool]: When master has finished reading/sending binlog it will
                      send EOF instead of blocking connection.
            only_events[List[str]]: Array of allowed events
            ignored_events[List[str]]: Array of ignored events
            log_file[str]: Set replication start log file
            log_pos[int]: Set replication start log pos (resume_stream should be
                     true)
            end_log_pos[int]: Set replication end log pos
            auto_position[str]: Use master_auto_position gtid to set position
            only_tables[List[str]]: An array with the tables you want to watch (only works
                         in binlog_format ROW)
            ignored_tables[List[str]]: An array with the tables you want to skip
            only_schemas[List[str]]: An array with the schemas you want to watch
            ignored_schemas[List[str]]: An array with the schemas you want to skip
            freeze_schema[bool]: If true do not support ALTER TABLE. It's faster.
            skip_to_timestamp[float]: Ignore all events until reaching specified timestamp.
            report_slave[ReportSlave]: Report slave in SHOW SLAVE HOSTS.
            slave_uuid[str]: Report slave_uuid or replica_uuid in SHOW SLAVE HOSTS(MySQL 8.0.21-) or
                        SHOW REPLICAS(MySQL 8.0.22+) depends on your MySQL version.
            fail_on_table_metadata_unavailable[bool]: Should raise exception if we
                                                can't get table information on row_events
            slave_heartbeat[float]: (seconds) Should master actively send heartbeat on
                             connection. This also reduces traffic in GTID
                             replication on replication resumption (in case
                             many event to skip in binlog). See
                             MASTER_HEARTBEAT_PERIOD in mysql documentation
                             for semantics
            is_mariadb[bool]: Flag to indicate it's a MariaDB server, used with auto_position
                    to point to Mariadb specific GTID.
            annotate_rows_event[bool]: Parameter value to enable annotate rows event in mariadb,
                    used with 'is_mariadb'
            ignore_decode_errors[bool]: If true, any decode errors encountered
                                  when reading column data will be ignored.
        """

        self.__connection_settings: Dict = connection_settings
        self.__connection_settings.setdefault("charset", "utf8")

        self.__connected_stream: bool = False
        self.__connected_ctl: bool = False
        self.__resume_stream: bool = resume_stream
        self.__blocking: bool = blocking
        self._ctl_connection_settings: Dict = ctl_connection_settings
        if ctl_connection_settings:
            self._ctl_connection_settings.setdefault("charset", "utf8")

        self.__only_tables: Optional[List[str]] = only_tables
        self.__ignored_tables: Optional[List[str]] = ignored_tables
        self.__only_schemas: Optional[List[str]] = only_schemas
        self.__ignored_schemas: Optional[List[str]] = ignored_schemas
        self.__freeze_schema: bool = freeze_schema
        self.__allowed_events: FrozenSet[str] = self._allowed_event_list(
            only_events, ignored_events, filter_non_implemented_events)
        self.__fail_on_table_metadata_unavailable: bool = fail_on_table_metadata_unavailable
        self.__ignore_decode_errors: bool = ignore_decode_errors

        # We can't filter on packet level TABLE_MAP and rotate event because
        # we need them for handling other operations
        self.__allowed_events_in_packet: FrozenSet[str] = frozenset(
            [TableMapEvent, RotateEvent]).union(self.__allowed_events)

        self.__server_id: int = server_id
        self.__use_checksum: bool = False

        # Store table meta information
        self.table_map: Dict = {}
        self.log_pos: Optional[int] = log_pos
        self.end_log_pos: Optional[int] = end_log_pos
        self.log_file: Optional[str] = log_file
        self.auto_position: Optional[str] = auto_position
        self.skip_to_timestamp: Optional[float] = skip_to_timestamp
        self.is_mariadb: bool = is_mariadb
        self.__annotate_rows_event: bool = annotate_rows_event

        if end_log_pos:
            self.is_past_end_log_pos: bool = False

        if report_slave:
            self.report_slave: ReportSlave = ReportSlave(report_slave)
        self.slave_uuid: Optional[str] = slave_uuid
        self.slave_heartbeat: Optional[float] = slave_heartbeat

        if pymysql_wrapper:
            self.pymysql_wrapper: Connection = pymysql_wrapper
        else:
            self.pymysql_wrapper: Optional[Union[Connection, Type[Connection]]] = pymysql.connect
        self.mysql_version: Tuple = (0, 0, 0)

    def close(self) -> None:
        if self.__connected_stream:
            self._stream_connection.close()
            self.__connected_stream: bool = False
        if self.__connected_ctl:
            # break reference cycle between stream reader and underlying
            # mysql connection object
            self._ctl_connection._get_table_information = None
            self._ctl_connection.close()
            self.__connected_ctl: bool = False

    def __connect_to_ctl(self) -> None:
        if not self._ctl_connection_settings:
            self._ctl_connection_settings: Dict[str, Any] = dict(self.__connection_settings)
        self._ctl_connection_settings["db"] = "information_schema"
        self._ctl_connection_settings["cursorclass"] = DictCursor
        self._ctl_connection_settings["autocommit"] = True
        self._ctl_connection: Connection = self.pymysql_wrapper(**self._ctl_connection_settings)
        self._ctl_connection._get_table_information = self.__get_table_information
        self.__connected_ctl: bool = True

    def __checksum_enabled(self) -> bool:
        """
        Return True if binlog-checksum = CRC32. Only for MySQL > 5.6
        """
        cur: Cursor = self._stream_connection.cursor()
        cur.execute("SHOW GLOBAL VARIABLES LIKE 'BINLOG_CHECKSUM'")
        result: Optional[Tuple[str, str]] = cur.fetchone()
        cur.close()

        if result is None:
            return False
        var, value = result[:2]
        if value == 'NONE':
            return False
        return True

    def _register_slave(self) -> None:
        if not self.report_slave:
            return

        packet: bytes = self.report_slave.encoded(self.__server_id)

        if pymysql.__version__ < LooseVersion("0.6"):
            self._stream_connection.wfile.write(packet)
            self._stream_connection.wfile.flush()
            self._stream_connection.read_packet()
        else:
            self._stream_connection._write_bytes(packet)
            self._stream_connection._next_seq_id = 1
            self._stream_connection._read_packet()

    def __connect_to_stream(self) -> None:
        # log_pos (4) -- position in the binlog-file to start the stream with
        # flags (2) BINLOG_DUMP_NON_BLOCK (0 or 1)
        # server_id (4) -- server id of this slave
        # log_file (string.EOF) -- filename of the binlog on the master
        self._stream_connection: Connection = self.pymysql_wrapper(**self.__connection_settings)

        self.__use_checksum: bool = self.__checksum_enabled()

        # If checksum is enabled we need to inform the server about the that
        # we support it
        if self.__use_checksum:
            cur: Cursor = self._stream_connection.cursor()
            cur.execute("SET @master_binlog_checksum= @@global.binlog_checksum")
            cur.close()

        if self.slave_uuid:
            cur: Cursor = self._stream_connection.cursor()
            cur.execute("SET @slave_uuid = %s, @replica_uuid = %s", (self.slave_uuid, self.slave_uuid))
            cur.close()

        if self.slave_heartbeat:
            # 4294967 is documented as the max value for heartbeats
            net_timeout: float = float(self.__connection_settings.get('read_timeout',
                                                               4294967))
            # If heartbeat is too low, the connection will disconnect before,
            # this is also the behavior in mysql
            heartbeat: float = float(min(net_timeout / 2., self.slave_heartbeat))
            if heartbeat > 4294967:
                heartbeat = 4294967

            # master_heartbeat_period is nanoseconds
            heartbeat: int = int(heartbeat * 1000000000)
            cur: Cursor = self._stream_connection.cursor()
            cur.execute("SET @master_heartbeat_period= %d" % heartbeat)
            cur.close()

        # When replicating from Mariadb 10.6.12 using binlog coordinates, a slave capability < 4 triggers a bug in
        # Mariadb, when it tries to replace GTID events with dummy ones. Given that this library understands GTID
        # events, setting the capability to 4 circumvents this error.
        # If the DB is mysql, this won't have any effect so no need to run this in a condition
        cur: Cursor = self._stream_connection.cursor()
        cur.execute("SET @mariadb_slave_capability=4")
        cur.close()

        self._register_slave()

        if not self.auto_position:
            if self.is_mariadb:
                prelude = self.__set_mariadb_settings()
            else:
                # only when log_file and log_pos both provided, the position info is
                # valid, if not, get the current position from master
                if self.log_file is None or self.log_pos is None:
                    cur: Cursor = self._stream_connection.cursor()
                    cur.execute("SHOW MASTER STATUS")
                    master_status: Optional[Tuple[str, int, Any]] = cur.fetchone()
                    if master_status is None:
                        raise BinLogNotEnabled()
                    self.log_file, self.log_pos = master_status[:2]
                    cur.close()

                prelude: bytes = struct.pack('<i', len(self.log_file) + 11) \
                          + bytes(bytearray([COM_BINLOG_DUMP]))

                if self.__resume_stream:
                    prelude += struct.pack('<I', self.log_pos)
                else:
                    prelude += struct.pack('<I', 4)

                flags: int = 0

                if not self.__blocking:
                    flags |= 0x01  # BINLOG_DUMP_NON_BLOCK
                prelude += struct.pack('<H', flags)

                prelude += struct.pack('<I', self.__server_id)
                prelude += self.log_file.encode()
        else:
            if self.is_mariadb:
                prelude = self.__set_mariadb_settings()
            else:
                # Format for mysql packet master_auto_position
                #
                # All fields are little endian
                # All fields are unsigned

                # Packet length   uint   4bytes
                # Packet type     byte   1byte   == 0x1e
                # Binlog flags    ushort 2bytes  == 0 (for retrocompatibilty)
                # Server id       uint   4bytes
                # binlognamesize  uint   4bytes
                # binlogname      str    Nbytes  N = binlognamesize
                #                                Zeroified
                # binlog position uint   4bytes  == 4
                # payload_size    uint   4bytes

                # What come next, is the payload, where the slave gtid_executed
                # is sent to the master
                # n_sid           ulong  8bytes  == which size is the gtid_set
                # | sid           uuid   16bytes UUID as a binary
                # | n_intervals   ulong  8bytes  == how many intervals are sent
                # |                                 for this gtid
                # | | start       ulong  8bytes  Start position of this interval
                # | | stop        ulong  8bytes  Stop position of this interval

                # A gtid set looks like:
                #   19d69c1e-ae97-4b8c-a1ef-9e12ba966457:1-3:8-10,
                #   1c2aad49-ae92-409a-b4df-d05a03e4702e:42-47:80-100:130-140
                #
                # In this particular gtid set,
                # 19d69c1e-ae97-4b8c-a1ef-9e12ba966457:1-3:8-10
                # is the first member of the set, it is called a gtid.
                # In this gtid, 19d69c1e-ae97-4b8c-a1ef-9e12ba966457 is the sid
                # and have two intervals, 1-3 and 8-10, 1 is the start position of
                # the first interval 3 is the stop position of the first interval.

                gtid_set: GtidSet = GtidSet(self.auto_position)
                encoded_data_size: int = gtid_set.encoded_length

                header_size: int = (2 +  # binlog_flags
                               4 +  # server_id
                               4 +  # binlog_name_info_size
                               4 +  # empty binlog name
                               8 +  # binlog_pos_info_size
                               4)  # encoded_data_size

                prelude: ByteString = b'' + struct.pack('<i', header_size + encoded_data_size) \
                          + bytes(bytearray([COM_BINLOG_DUMP_GTID]))

                flags: int = 0
                if not self.__blocking:
                    flags |= 0x01  # BINLOG_DUMP_NON_BLOCK
                flags |= 0x04  # BINLOG_THROUGH_GTID

                # binlog_flags (2 bytes)
                # see:
                #  https://dev.mysql.com/doc/internals/en/com-binlog-dump-gtid.html
                prelude += struct.pack('<H', flags)

                # server_id (4 bytes)
                prelude += struct.pack('<I', self.__server_id)
                # binlog_name_info_size (4 bytes)
                prelude += struct.pack('<I', 3)
                # empty_binlog_namapprovale (4 bytes)
                prelude += b'\0\0\0'
                # binlog_pos_info (8 bytes)
                prelude += struct.pack('<Q', 4)

                # encoded_data_size (4 bytes)
                prelude += struct.pack('<I', gtid_set.encoded_length)
                # encoded_data
                prelude += gtid_set.encoded()

        if pymysql.__version__ < LooseVersion("0.6"):
            self._stream_connection.wfile.write(prelude)
            self._stream_connection.wfile.flush()
        else:
            self._stream_connection._write_bytes(prelude)
            self._stream_connection._next_seq_id = 1
        self.__connected_stream: bool = True

    def __set_mariadb_settings(self) -> bytes:
        # https://mariadb.com/kb/en/5-slave-registration/
        cur: Cursor = self._stream_connection.cursor()
        if self.auto_position != None:
            cur.execute("SET @slave_connect_state='%s'" % self.auto_position)
        cur.execute("SET @slave_gtid_strict_mode=1")
        cur.execute("SET @slave_gtid_ignore_duplicates=0")
        cur.close()

        # https://mariadb.com/kb/en/com_binlog_dump/
        header_size: int = (
                4 +  # binlog pos
                2 +  # binlog flags
                4 +  # slave server_id,
                4  # requested binlog file name , set it to empty
        )

        prelude: bytes = struct.pack('<i', header_size) + bytes(bytearray([COM_BINLOG_DUMP]))

        # binlog pos
        prelude += struct.pack('<i', 4)

        flags: int = 0

        # Enable annotate rows event
        if self.__annotate_rows_event:
            flags |= 0x02  # BINLOG_SEND_ANNOTATE_ROWS_EVENT

        if not self.__blocking:
            flags |= 0x01  # BINLOG_DUMP_NON_BLOCK

        # binlog flags
        prelude += struct.pack('<H', flags)

        # server id (4 bytes)
        prelude += struct.pack('<I', self.__server_id)

        # empty_binlog_name (4 bytes)
        prelude += b'\0\0\0\0'

        return prelude

    def fetchone(self) -> Union[BinLogPacketWrapper, None]:
        while True:
            if self.end_log_pos and self.is_past_end_log_pos:
                return None

            if not self.__connected_stream:
                self.__connect_to_stream()

            if not self.__connected_ctl:
                self.__connect_to_ctl()

            try:
                if pymysql.__version__ < LooseVersion("0.6"):
                    pkt: MysqlPacket = self._stream_connection.read_packet()
                else:
                    pkt: MysqlPacket = self._stream_connection._read_packet()
            except pymysql.OperationalError as error:
                code, message = error.args
                if code in MYSQL_EXPECTED_ERROR_CODES:
                    self._stream_connection.close()
                    self.__connected_stream = False
                    continue
                raise

            if pkt.is_eof_packet():
                self.close()
                return None

            if not pkt.is_ok_packet():
                continue

            binlog_event: BinLogPacketWrapper = BinLogPacketWrapper(pkt, self.table_map,
                                               self._ctl_connection,
                                               self.mysql_version,
                                               self.__use_checksum,
                                               self.__allowed_events_in_packet,
                                               self.__only_tables,
                                               self.__ignored_tables,
                                               self.__only_schemas,
                                               self.__ignored_schemas,
                                               self.__freeze_schema,
                                               self.__fail_on_table_metadata_unavailable,
                                               self.__ignore_decode_errors)

            if binlog_event.event_type == ROTATE_EVENT:
                self.log_pos = binlog_event.event.position
                self.log_file = binlog_event.event.next_binlog
                # Table Id in binlog are NOT persistent in MySQL - they are in-memory identifiers
                # that means that when MySQL master restarts, it will reuse same table id for different tables
                # which will cause errors for us since our in-memory map will try to decode row data with
                # wrong table schema.
                # The fix is to rely on the fact that MySQL will also rotate to a new binlog file every time it
                # restarts. That means every rotation we see *could* be a sign of restart and so potentially
                # invalidates all our cached table id to schema mappings. This means we have to load them all
                # again for each logfile which is potentially wasted effort but we can't really do much better
                # without being broken in restart case
                self.table_map: Dict = {}
            elif binlog_event.log_pos:
                self.log_pos = binlog_event.log_pos

            if self.end_log_pos and self.log_pos >= self.end_log_pos:
                # We're currently at, or past, the specified end log position.
                self.is_past_end_log_pos = True

            # This check must not occur before clearing the ``table_map`` as a
            # result of a RotateEvent.
            #
            # The first RotateEvent in a binlog file has a timestamp of
            # zero.  If the server has moved to a new log and not written a
            # timestamped RotateEvent at the end of the previous log, the
            # RotateEvent at the beginning of the new log will be ignored
            # if the caller provided a positive ``skip_to_timestamp``
            # value.  This will result in the ``table_map`` becoming
            # corrupt.
            #
            # https://dev.mysql.com/doc/internals/en/event-data-for-specific-event-types.html
            # From the MySQL Internals Manual:
            #
            #   ROTATE_EVENT is generated locally and written to the binary
            #   log on the master. It is written to the relay log on the
            #   slave when FLUSH LOGS occurs, and when receiving a
            #   ROTATE_EVENT from the master. In the latter case, there
            #   will be two rotate events in total originating on different
            #   servers.
            #
            #   There are conditions under which the terminating
            #   log-rotation event does not occur. For example, the server
            #   might crash.
            if self.skip_to_timestamp and binlog_event.timestamp < self.skip_to_timestamp:
                continue

            if binlog_event.event_type == TABLE_MAP_EVENT and \
                    binlog_event.event is not None:
                self.table_map[binlog_event.event.table_id] = \
                    binlog_event.event.get_table()

            # event is none if we have filter it on packet level
            # we filter also not allowed events
            if binlog_event.event is None or (binlog_event.event.__class__ not in self.__allowed_events):
                continue

            if binlog_event.event_type == FORMAT_DESCRIPTION_EVENT:
                self.mysql_version = binlog_event.event.mysql_version

            return binlog_event.event

    def _allowed_event_list(self, only_events: Optional[List[str]], ignored_events: Optional[List[str]],
                            filter_non_implemented_events: bool) -> FrozenSet[str]:
        if only_events is not None:
            events = set(only_events)
        else:
            events = set((
                QueryEvent,
                RotateEvent,
                StopEvent,
                FormatDescriptionEvent,
                XAPrepareEvent,
                XidEvent,
                GtidEvent,
                BeginLoadQueryEvent,
                ExecuteLoadQueryEvent,
                UpdateRowsEvent,
                WriteRowsEvent,
                DeleteRowsEvent,
                TableMapEvent,
                HeartbeatLogEvent,
                NotImplementedEvent,
                MariadbGtidEvent,
                RowsQueryLogEvent,
                MariadbAnnotateRowsEvent,
                RandEvent,
                MariadbStartEncryptionEvent,
                MariadbGtidListEvent,
                MariadbBinLogCheckPointEvent
            ))
        if ignored_events is not None:
            for e in ignored_events:
                events.remove(e)
        if filter_non_implemented_events:
            try:
                events.remove(NotImplementedEvent)
            except KeyError:
                pass
        return frozenset(events)

    def __get_table_information(self, schema: str, table: str) -> List[Dict[str, Any]]:
        for i in range(1, 3):
            try:
                if not self.__connected_ctl:
                    self.__connect_to_ctl()

                cur: Cursor = self._ctl_connection.cursor()
                cur.execute("""
                    SELECT
                        COLUMN_NAME, COLLATION_NAME, CHARACTER_SET_NAME,
                        COLUMN_COMMENT, COLUMN_TYPE, COLUMN_KEY, ORDINAL_POSITION,
                        DATA_TYPE, CHARACTER_OCTET_LENGTH
                    FROM
                        information_schema.columns
                    WHERE
                        table_schema = %s AND table_name = %s
                    """, (schema, table))
                result: List = sorted(cur.fetchall(), key=lambda x: x['ORDINAL_POSITION'])
                cur.close()

                return result
            except pymysql.OperationalError as error:
                code, message = error.args
                if code in MYSQL_EXPECTED_ERROR_CODES:
                    self.__connected_ctl = False
                    continue
                else:
                    raise error

    def __iter__(self) -> Iterator[Union[BinLogPacketWrapper, None]]:
        return iter(self.fetchone, None)
