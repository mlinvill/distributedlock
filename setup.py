"""
setup.py
"""
from setuptools import setup

setup(name='distributedlock',
      version='0.1',
      description='Distributed lock implementation using pySyncObj',
      url='https://github.com/mlinvill/distributedlock.git',
      author='Mark Linvill',
      author_email='mlinvill@purdue.edu',
      license='MIT',
      packages=['distributedlock'],
      install_requires=[
          'click',
          'python-dotenv',
          'pysyncobj',
          'rich',
      ],
      zip_safe=False)
