#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Dump all replication events from a remote mysql server
#

from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.event import MariadbAnnotateRowsEvent
from antlr4 import FileStream, CommonTokenStream
from pymysqlreplication.queryparser.MySqlParser import MySqlParser
from pymysqlreplication.queryparser.MySqlLexer import MySqlLexer
from antlr4 import ParseTreeWalker
from pymysqlreplication.queryparser.MySqlParserListener import MySqlParserListener


MYSQL_SETTINGS = {"host": "127.0.0.1", "port": 3308, "user": "root", "passwd": ""}


class MyMySQLListener(MySqlParserListener):
    def enterEveryRule(self, ctx):
        print("Type name:{} ,Entered:{}".format(type(ctx), ctx.getText()))

    def exitEveryRule(self, ctx):
        pass
        # print("Exited:", ctx.getText())


def main():
    # server_id is your slave identifier, it should be unique.
    # set blocking to True if you want to block and wait for the next event at
    # the end of the stream
    stream = BinLogStreamReader(
        connection_settings=MYSQL_SETTINGS,
        server_id=3,
        blocking=True,
        is_mariadb=True,
        annotate_rows_event=True,
    )

    for binlogevent in stream:
        if type(binlogevent) == MariadbAnnotateRowsEvent:
            input_stream = FileStream(binlogevent.sql_statement)
            lexer = MySqlLexer(input_stream)
            token_stream = CommonTokenStream(lexer)
            parser = MySqlParser(token_stream)

            # This is the entry point to your parser, replace root() with the actual root rule of your grammar.
            # Check MySQLParser.py to find the name of the root rule (it's often the first rule defined in the .g4 file)
            tree = parser.root()

            walker = ParseTreeWalker()
            my_listener = MyMySQLListener()

            walker.walk(my_listener, tree)
        binlogevent.dump()

    stream.close()


if __name__ == "__main__":
    main()
