from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsVerifiedSeller(BasePermission):
    """Allows write access only to users whose KYC is verified and who hold
    a selling role (seller/journalist/media_house)."""

    message = "Your National ID verification must be approved before you can sell news."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_authenticated and request.user.can_sell)


class IsListingOwner(BasePermission):
    """Object-level permission: the seller who owns a listing, or any
    moderator/admin, may write to it. Safe methods are always allowed
    (list/retrieve visibility is handled separately by the view's queryset)."""

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if obj.seller_id == user.id:
            return True
        return bool(user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff)


class IsModeratorOrAbove(BasePermission):
    message = "Moderator or admin role required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff)
        )


class IsAdminOrAbove(BasePermission):
    message = "Admin role required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.role in {user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff)
        )


class IsSuperAdmin(BasePermission):
    message = "Super admin role required."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.role == user.Role.SUPER_ADMIN or user.is_superuser))


class IsNotSuspendedOrBanned(BasePermission):
    """Blanket check to reject any write from a suspended/banned account,
    regardless of what else the view allows."""

    message = "Your account is currently suspended or banned."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return True  # let authentication/other permissions handle this
        return not (user.is_banned or user.is_suspended)
