from http.cookiejar import Cookie, CookieJar


def jar_from_records(records: list[dict]) -> CookieJar:
    jar = CookieJar()
    for record in records:
        domain = record["domain"]
        expires = record.get("expires")
        expires = int(expires) if expires is not None and expires >= 0 else None
        rest = dict(record.get("rest", {}))
        if record.get("httpOnly"):
            rest["HttpOnly"] = None
        if record.get("sameSite") is not None:
            rest["SameSite"] = record["sameSite"]
        jar.set_cookie(Cookie(
            version=record.get("version", 0), name=record["name"], value=record["value"],
            port=record.get("port"), port_specified=record.get("port_specified", False),
            domain=domain,
            domain_specified=record.get("domain_specified", not record.get("hostOnly", not domain.startswith("."))),
            domain_initial_dot=record.get("domain_initial_dot", domain.startswith(".")),
            path=record.get("path", "/"), path_specified=record.get("path_specified", True),
            secure=record.get("secure", False), expires=expires,
            discard=record.get("discard", expires is None),
            comment=record.get("comment"), comment_url=record.get("comment_url"),
            rest=rest, rfc2109=record.get("rfc2109", False),
        ))
    return jar


def jar_to_records(jar: CookieJar) -> list[dict]:
    return [
        {
            "version": cookie.version, "name": cookie.name, "value": cookie.value,
            "port": cookie.port, "port_specified": cookie.port_specified,
            "domain": cookie.domain, "domain_specified": cookie.domain_specified,
            "domain_initial_dot": cookie.domain_initial_dot,
            "path": cookie.path, "path_specified": cookie.path_specified,
            "secure": cookie.secure, "expires": cookie.expires, "discard": cookie.discard,
            "comment": cookie.comment, "comment_url": cookie.comment_url,
            "rest": dict(cookie._rest), "rfc2109": cookie.rfc2109,
        }
        for cookie in jar
    ]
