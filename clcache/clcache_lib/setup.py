from setuptools import setup, find_packages

setup(
    name="clcache-lib",
    version="4.4.4j",
    author="Various",
    author_email="Various",
    packages=find_packages(exclude=('clcache_lib',)),
    scripts=[],
    url="https://github.com/dgehri/clcache",
    license="LICENSE.txt",
    description="ClCache Library",
    long_description=open("../doc/README.txt").read(),
    install_requires=[],
)
