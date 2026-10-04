"""CHIYO integration adapters (CHIYO-PROD-03).

This package holds the adapters that connect the *real* production components
(the Alpha passive-chat application, the World/Body service, the Memory runtime
and the Hermes gateway plugin) to the CHIYO core runtime.

Design rules for every adapter in here:

* It observes or delegates; it never replaces another domain's canonical writer.
* It fails open on the chat path (a chat turn must never break because of us) and
  fails closed on anything that could produce an external effect.
* It carries the *real* identity of the source event, so duplicate delivery can
  never produce a second semantic transition.
"""
