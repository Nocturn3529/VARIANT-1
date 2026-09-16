"""Persistent CPython runtime package.

Host code imports concrete owners from their modules. Keeping this package
initializer empty prevents the separately frozen worker from importing host
composition or provider dependencies.
"""
