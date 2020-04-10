Building the Docs
=================

Although documentation for the OpenFF Evaluator is `readily available online
<https://property-estimator.readthedocs.io/en/latest/>`_, it is sometimes useful
to build a local version such as when

- developing new pages which you wish to preview without having to wait
  for ReadTheDocs to finish building.

- debugging errors which occur when building on ReadTheDocs.

In these cases, the docs can be built locally by doing the following::

    git clone https://github.com/openforcefield/openff-evaluator.git
    cd openff-evaluator/docs
    conda env create --name openff-evaluator-docs --file environment.yaml
    conda activate openff-evaluator-docs
    rm -rf api && make clean && make html

The above will yield a new directory named `_build` which will contain the built
html files which can be viewed in your local browser.