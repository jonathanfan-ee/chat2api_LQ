import logging

import os

# 从环境变量获取日志级别，默认为 INFO
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s | %(levelname)s | %(message)s'
)


class Logger:
    @staticmethod
    def info(message):
        logging.info(str(message))

    @staticmethod
    def warning(message):
        logging.warning("\033[0;33m" + str(message) + "\033[0m")

    @staticmethod
    def error(message):
        logging.error("\033[0;31m" + "-" * 50 + '\n| ' + str(message) + "\033[0m" + "\n" + "└" + "-" * 80)

    @staticmethod
    def debug(message):
        logging.debug("\033[0;37m" + str(message) + "\033[0m")


logger = Logger()
