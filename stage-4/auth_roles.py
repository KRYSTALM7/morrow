"""Role-based authorization for Morrow Stage 4."""

def require_role(role):
    """
    Check if user has required role.
    Returns (has_role: bool, error_message: str | None)
    """
    def check(user):
        if not user:
            return False, "User not found"
        
        roles = user.get("roles", [])
        if role not in roles:
            return False, f"{role} role required"
        
        return True, None
    return check


def require_analyst(user):
    """
    Check if user has PAYMENT_ANALYST role AND approved status.
    Returns (has_access: bool, error_message: str | None)
    """
    if not user:
        return False, "User not found"
    
    roles = user.get("roles", [])
    if "PAYMENT_ANALYST" not in roles:
        return False, "Payment analyst role required"
    
    analyst_status = user.get("analyst_status", "pending")
    if analyst_status != "approved":
        return False, f"Analyst account {analyst_status}"
    
    return True, None


def require_admin(user):
    """
    Check if user has PLATFORM_ADMIN role.
    Returns (has_access: bool, error_message: str | None)
    """
    if not user:
        return False, "User not found"
    
    roles = user.get("roles", [])
    if "PLATFORM_ADMIN" not in roles:
        return False, "Platform admin role required"
    
    return True, None


# Valid roles that can be assigned during signup
VALID_SIGNUP_ROLES = {"CUSTOMER", "PAYMENT_ANALYST"}

# All valid roles
ALL_ROLES = {"CUSTOMER", "PAYMENT_ANALYST", "PLATFORM_ADMIN"}

# Valid analyst statuses
ANALYST_STATUSES = {"pending", "approved", "suspended", "rejected"}

# Valid admin action types (for audit log)
ADMIN_ACTION_TYPES = {
    "approve_analyst",
    "suspend_analyst",
    "reinstate_analyst",
    "reject_analyst",
    "assign_role",
    "remove_role",
    "create_admin"
}
