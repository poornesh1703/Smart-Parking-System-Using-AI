from flask import request, url_for

TRAILS = {
    "views.home":                 [("Home", None)],
    "auth.login":                 [("Home", "views.home"), ("Login", None)],
    "auth.signup":                [("Home", "views.home"), ("Sign Up", None)],
    "views.admin_dashboard":      [("Home", "views.home"), ("Admin", None)],
    "views.admin_parking_list":   [("Home", "views.home"), ("Admin", "views.admin_dashboard"), ("Parking Lots", None)],
    "views.admin_parking_new":    [("Home", "views.home"), ("Admin", "views.admin_dashboard"),
                                   ("Parking Lots", "views.admin_parking_list"), ("Add New", None)],
    "views.admin_parking_detail": [("Home", "views.home"), ("Admin", "views.admin_dashboard"),
                                   ("Parking Lots", "views.admin_parking_list"), ("Detail", None)],
}

def build_breadcrumb(**ctx):
    ep = request.endpoint or ""
    trail = TRAILS.get(ep, [("Home", "views.home")])
    items = []
    for label, endpoint in trail:
        if label == "Detail" and "lot" in ctx and getattr(ctx["lot"], "name", None):
            label = ctx["lot"].name
        href = url_for(endpoint) if endpoint else None
        items.append({"label": label, "href": href, "active": False})
    if items:
        items[-1]["active"] = True
        items[-1]["href"] = None
    return items
