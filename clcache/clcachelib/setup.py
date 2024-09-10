from setuptools import setup, find_packages

setup(
    name="clcachelib",
    version="4.4.34",
    author="Various",
    author_email="Various",
    packages=find_packages(exclude=('clcachelib',)),
    scripts=[],
    url="https://github.com/dgehri/clcache",
    license="LICENSE.txt",
    description="ClCache Library",
    long_description=open("../doc/README.txt").read(),
    install_requires=[],
)
