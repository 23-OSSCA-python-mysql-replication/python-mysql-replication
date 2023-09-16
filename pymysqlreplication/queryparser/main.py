from antlr4 import FileStream, CommonTokenStream
from MySqlParser import MySqlParser
from MySqlLexer import MySqlLexer
from antlr4 import ParseTreeWalker
from MySqlParserListener import MySqlParserListener


class MyMySQLListener(MySqlParserListener):
    def enterEveryRule(self, ctx):
        print("Type name:{} ,Entered:{}".format(type(ctx), ctx.getText()))

    def exitEveryRule(self, ctx):
        pass
        # print("Exited:", ctx.getText())


def main():
    # Replace 'example.sql' with the path to the SQL file you want to parse
    input_stream = FileStream(
        "/Users/cucuridas/Desktop/oss/python-mysql-replication/pymysqlreplication/queryparser/example.sql"
    )
    lexer = MySqlLexer(input_stream)
    token_stream = CommonTokenStream(lexer)
    parser = MySqlParser(token_stream)

    # This is the entry point to your parser, replace root() with the actual root rule of your grammar.
    # Check MySQLParser.py to find the name of the root rule (it's often the first rule defined in the .g4 file)
    tree = parser.root()

    walker = ParseTreeWalker()
    my_listener = MyMySQLListener()

    walker.walk(my_listener, tree)

    # If you want to do something with the tree, you can do it here.
    # For instance, you could implement a Listener or a Visitor to walk the tree and do something useful.
    pass


if __name__ == "__main__":
    main()
