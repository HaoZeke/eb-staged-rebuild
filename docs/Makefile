# Build the HTML site. Graphviz (`dot`) is required for the staging diagram.
SPHINXOPTS = -W
SPHINXBUILD = sphinx-build
SOURCEDIR = .
BUILDDIR = _build

.PHONY: html clean

html:
	$(SPHINXBUILD) $(SPHINXOPTS) -b html $(SOURCEDIR) $(BUILDDIR)/html

clean:
	rm -rf $(BUILDDIR)
