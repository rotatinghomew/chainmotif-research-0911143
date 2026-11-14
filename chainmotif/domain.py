from collections import defaultdict


def event_lines(catalog):
    lines = defaultdict(list)
    for record in catalog.rows("events"):
        lines[record["shipment_id"]].append(record)
    return {shipment: sorted(records, key=lambda record: (record["occurred_at"], record["id"])) for shipment, records in lines.items()}


def overview(catalog):
    lines = event_lines(catalog)
    first = sorted(lines)[0]
    return {"货批数": len(catalog.rows("shipments")), "事件数": len(catalog.rows("events")), "首个历程": lines[first], "有观测缺口数": sum(record["gap_from"] is not None for record in catalog.rows("observation_windows"))}
