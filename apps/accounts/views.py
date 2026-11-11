from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render


def landing(request):
    if request.user.is_authenticated:
        return redirect("jobs:dashboard")
    return render(request, "landing.html")


@login_required
def about(request):
    return render(request, "accounts/about.html")
