def pytest_addoption(parser):
    parser.addoption(
        '--dbms', action='store', default='mysql-5'
    )