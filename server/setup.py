import os

from setuptools import setup

setup(version=os.environ.get("SCANOCR_VERSION", "0.0.0.dev0+unknown"))
