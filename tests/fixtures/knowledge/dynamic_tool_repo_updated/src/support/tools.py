"""Support tools after the refund policy was removed."""


class LookupOrderStatus:
    def _run(self, order_id: str) -> dict:
        return {"status": "delivered", "order_id": order_id}


class IssueRefund:
    def _run(self, order_id: str) -> dict:
        return {"refunded": True, "order_id": order_id}


def _initialize_tools():
    return {
        "lookup_order_status": LookupOrderStatus(),
        "issue_refund": IssueRefund(),
    }
