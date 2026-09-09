"""Support tools registered in a runtime dict (A8)."""


class CheckRefundEligibility:
    def _run(self, order_id: str) -> dict:
        """Eligible delivered orders may receive a refund."""
        return {"eligible": True, "order_id": order_id}


class LookupOrderStatus:
    def _run(self, order_id: str) -> dict:
        return {"status": "delivered", "order_id": order_id}


class IssueRefund:
    def _run(self, order_id: str) -> dict:
        return {"refunded": True, "order_id": order_id}


def _initialize_tools():
    return {
        "check_refund_eligibility": CheckRefundEligibility(),
        "lookup_order_status": LookupOrderStatus(),
        "issue_refund": IssueRefund(),
    }
