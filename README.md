# Certcentral

Certcentral is a Python 3 application that is to be used to centrally request configured TLS
certificates from ACME servers, then make them available to authorised API users. The API is
intended to sit behind uwsgi and nginx running TLS client certificate checking based on a private
CA. It can support http-01 and dns-01 challenges.

Certcentral itself consists of two parts:
* The backend in certcentral.py, which is responsible for generating initial certificates and then
  replacing them with live ones from the specified ACME server.
* The API in api.py/uwsgi.py, which is responsible for taking requests from users,
  and distributing the certificates saved by the backend.

It is intended for use in multi-server environments where any one of several actual servers with no
shared filesystem are required to terminate TLS connections, where it is not feasible to have each
server requesting their own certificates from ACME servers.

One variant of the API permits simple use by puppet.
It is hoped that eventually this will be used to handle certificates for wikipedia.org and co.

The license in use is GPL v3+ and the main developers are Alex Monk <krenair@gmail.com> and Valentin
Gutierrez <vgutierrez@wikimedia.org>.
