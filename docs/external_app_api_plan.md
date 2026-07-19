Recommended shape
A dedicated app/namespace, e.g. urbanlens/external_api/ mounted at /api/external/v1/, with its own serializers and views — thin, purpose-built, calling into the same service/model layer your internal API uses, but never inheriting from your internal viewsets. This also gives you independent versioning: you can break your internal API tomorrow without touching a public contract someone else's app depends on.
Auth: scoped API keys (PAT-style), not OAuth2 — at least to start. A model like:
pythonclass ExternalApiKey(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)  # user-facing label, e.g. "Zapier"
    key_hash = models.CharField(max_length=128, unique=True)  # store hash, not plaintext
    prefix = models.CharField(max_length=8)  # shown in UI for identification
    scopes = models.JSONField(default=list)  # ["pins:read", "visits:write"]
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
User generates a key from their settings page, picks scopes from a checklist, gets the plaintext once (never stored), gives it to the external app as a bearer token. A custom authentication.py class looks it up by hash, and a scope-checking permission class gates each view against required_scopes = {"pins:read"}.
This is the right call if the model is "a user connects their own account to a handful of tools" (Zapier-style automation, a browser extension, a script) — no third-party developer registration, no consent screens, no redirect flows. 

Practical details worth nailing down early

Scope granularity matches your domain, not CRUD verbs generically — pins:read, pins:write, trips:read, visits:write, wiki:read rather than one blanket read/write. Given the privacy angle you've built E2EE messaging around, I'd keep DMs and any encrypted fields entirely out of scope for external API access regardless of what the user picks — don't offer a scope for it at all rather than trusting scope selection to protect it.
Rate limiting per key, not just per user — a misbehaving external app shouldn't be able to hammer the API under the umbrella of a legitimate user's account.
Audit log of key usage (endpoint, timestamp, at minimum) so a user can see what an app has actually been doing with its access — this matters more for trust than for you, since you can't vet every third-party app that shows up.
Revocation needs to be instant and visible — a settings page listing active keys with "last used" and a revoke button, no caching that would let a revoked key work for another few minutes.

Entirely separate at the REST API layer — but not separate at the Django app/business-logic layer. That distinction matters, so let me be precise about what's shared and what's not.
Not shared: viewsets, serializers, URL routes, permission classes tied to your internal API. The external API gets its own views.py, serializers.py, urls.py, authentication.py in its own app. It does not subclass or extend your PinViewSet or reuse PinSerializer. If you change what fields your frontend's pin serializer returns, the external API is untouched, and vice versa.
Shared: your models, your .objects managers/querysets, and ideally a service/selector layer if you have one (e.g. pins.services.create_pin(user, ...)). Both APIs are just different doors into the same underlying data and logic — they call the same functions, they just expose different, independently-shaped views into them.
So concretely, if you have:
urbanlens/
  dashboard/
    models.py
    serializers.py      # internal, full-featured
    views.py             # internal viewsets
    services.py          # e.g. create_pin(), get_visible_pins_for_user()
  external_api/
    serializers.py        # separate, deliberately minimal
    views.py               # separate, calls pins.services
    authentication.py     # API key auth
    permissions.py         # scope checks
    urls.py
external_api/views.py might do something like:
pythonclass ExternalPinListView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasScope]
    required_scopes = {"pins:read"}

    def get(self, request):
        pins = pins.services.get_visible_pins_for_user(request.user)
        return Response(ExternalPinSerializer(pins, many=True).data)
It calls into the same pins app's logic your internal viewset uses, but has its own serializer (ExternalPinSerializer, likely a strict subset of fields) and its own auth/permission stack.
Why this is worth the extra app rather than just writing new viewsets under /api/v1/: it makes the boundary a structural fact, not a convention someone (including future-you at 1am) has to remember to maintain. There's no viewset where you have to check "wait, is this endpoint reachable via API key auth too?" — external-facing surface only exists in one place, and it's obvious by directory alone what is and isn't exposed to third parties.
The tradeoff is some duplication — you'll write a second, smaller serializer for pins that overlaps with the internal one. That's the right tradeoff here: the two serializers should drift independently (internal can add fields freely for your mobile app; external stays deliberately conservative and versioned), so shared serializer code would just be fighting that.