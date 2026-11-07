"""The shopping agent. Untrusted by design.

It can search products, read product pages, and propose ONE PurchaseIntent to
the mandate service over HTTP. It holds a mandate token and nothing else: no
PayPal credentials, no payment token, no admin token (I1). It never imports
the `mandate` package; a test enforces that.
"""
