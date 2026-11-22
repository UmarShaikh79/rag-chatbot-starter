"""Your workspace. Imported after every built-in module, so anything registered here wins.

Examples:

    from chatbot.core.registry import registry

    @registry.register("source", "crm")          # ingestion.sources: [{type: crm, ...}]
    def make_crm_source(cfg, root_dir): ...

    @registry.register("retriever", "dense")     # replace the default retriever
    def make_my_retriever(embedder, store, alias, **kwargs): ...   # kwargs: settings=...

Import each module you add here (``from chatbot.domain import my_module``). Step by step:
docs/tutorial.md; every kind of part: docs/extending.md.
"""
