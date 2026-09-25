"""Native directory-plugin adapter; imports stay within the deployed copy."""


def register(ctx):
    from .hermes_delegate_routing import register as register_plugin

    register_plugin(ctx)
