"""A synthetic health system for Talk: a hospital and pharmacy network in Lagos that every part of the demo can call.

Everything in it is made up: the facilities, the numbers, the queues, the stock. It is built the way a real provider's system is reached, as a separate
service that Talk calls over a signed HTTP connector and that stays the source of truth for its own records. Swapping in a real hospital means a new
connector and new data, not a new design. See docs/design.md.
"""
