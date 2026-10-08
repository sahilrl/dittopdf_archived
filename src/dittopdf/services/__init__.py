"""PDF inspection, comparison and copying services.

Nothing in this package knows about Flask: the web layer (``dittopdf.web``)
only calls :func:`inspector.inspect_pdf`, :func:`comparison.compare` and
:func:`copier.copy_properties`, and renders the plain dictionaries they return.
"""
