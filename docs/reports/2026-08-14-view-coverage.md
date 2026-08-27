# View-layer coverage: which request handlers never run

Generated 2026-08-14 from `coverage run` over the **full** suite (10,742 tests passing),
scoped to `dashboard/controllers/` and `dashboard/external_api/`. This answers the question left
open in the 2026-08-11 audit, which had until now only been approximated by counting routes
referenced in tests - an upper bound that could not distinguish "imported" from "executed".

- **80%** of 22,081 statements in the view layer are executed
- **208 of 1795 callables (11%) never execute at all**
- **100 of those 208 are HTTP write handlers** (`post`/`delete`/`put`/`patch`),
  totalling **1,217 statements** of data-mutating code that no test reaches

That last figure is the one that matters. Untested read paths render a wrong page; untested write
paths corrupt data, and half of what is unexercised here is a write path.

## Corroboration

`PinController.upload_takeout` (39 statements) appears both here and in the separate sweep for
routes with no discoverable caller (`pin.upload.takeout`, see `docs/PROBLEMS.md`). Two independent
instruments agreeing makes it the strongest candidate for either removal or a first test.

## Never-executed callables, by file

### `controllers/userprofile.py` - 19 callables, 190 statements

- `SocialLinkVerifyView._check_url` - 18 stmts
- `EditProfileView._add_email` - 16 stmts
- `ProfileEmailVerifyView.get` - 16 stmts
- `ProfileFieldUpdateView._save_avatar_gravatar` - 15 stmts
- `_send_profile_email_verification` - 15 stmts
- `ProfileTrustView.post` - 15 stmts **(write)**
- `ProfileNicknameView.post` - 15 stmts **(write)**
- `EditProfileView._save_profile` - 11 stmts
- `ProfileLabelToggleView.post` - 11 stmts **(write)**
- `ViewProfileView.post` - 10 stmts **(write)**
- `EditProfileView._save_discord` - 9 stmts
- `ProfileNoteView.post` - 9 stmts **(write)**
- `EditProfileView._resend_email_verification` - 6 stmts
- `ProfileNoteEditView.post` - 6 stmts **(write)**
- `ProfileNoteDeleteView.post` - 5 stmts **(write)**
- `_authenticated_profile` - 4 stmts
- `ProfileFieldUpdateView._save_avatar_emoji` - 3 stmts
- `EditProfileView._remove_email` - 3 stmts
- `EditProfileView._emails_response` - 3 stmts

### `controllers/consensus.py` - 16 callables, 174 statements

- `ConsensusPhotoUploadView.post` - 31 stmts **(write)**
- `ConsensusStartView.post` - 24 stmts **(write)**
- `ConsensusAnswerView.post` - 15 stmts **(write)**
- `ConsensusVoteView.post` - 15 stmts **(write)**
- `_parse_answer_value` - 13 stmts
- `ConsensusBeginView.post` - 12 stmts **(write)**
- `ConsensusSkipView.post` - 12 stmts **(write)**
- `ConsensusInviteView.post` - 11 stmts **(write)**
- `ConsensusRoundView.get` - 9 stmts
- `ConsensusJoinView.post` - 7 stmts **(write)**
- `ConsensusEndSessionView.post` - 7 stmts **(write)**
- `_participant_session` - 4 stmts
- `_joined_participant` - 4 stmts
- `ConsensusChatHistoryView.get` - 4 stmts
- `ConsensusLobbyView.get` - 3 stmts
- `ConsensusSummaryView.get` - 3 stmts

### `controllers/labels.py` - 10 callables, 146 statements

- `LabelBulkConvertView.post` - 36 stmts **(write)**
- `LabelBulkEditView.post` - 33 stmts **(write)**
- `LabelMergeView.post` - 19 stmts **(write)**
- `_apply_bulk_fields` - 14 stmts
- `LabelReorderView.post` - 13 stmts **(write)**
- `LabelBulkConvertView._resolved_target_kind` - 9 stmts
- `LabelCustomizeView.get` - 9 stmts
- `_safe_int` - 8 stmts
- `LabelCustomizeView._customize_ctx` - 4 stmts
- `_parse_bulk_payload` - 1 stmts

### `external_api/views_messaging.py` - 10 callables, 132 statements

- `GroupMembersView.delete` - 26 stmts **(write)**
- `GroupPinShareView.post` - 23 stmts **(write)**
- `MessageReactionView.post` - 17 stmts **(write)**
- `MessageDetailView.delete` - 17 stmts **(write)**
- `GroupsView.post` - 15 stmts **(write)**
- `GroupDetailView.patch` - 14 stmts **(write)**
- `GroupReadView.post` - 8 stmts **(write)**
- `MessageThreadReadView.post` - 7 stmts **(write)**
- `GroupsView.get` - 3 stmts
- `_group_payload` - 2 stmts

### `controllers/site_admin.py` - 13 callables, 111 statements

- `SiteAdminUsersView.post` - 35 stmts **(write)**
- `DevToolbarToggleThemeView.post` - 12 stmts **(write)**
- `DevToolbarToggleMapDarkModeView.post` - 12 stmts **(write)**
- `DevToolbarResetOnboardingView.post` - 11 stmts **(write)**
- `DevToolbarClearSessionView.post` - 7 stmts **(write)**
- `SiteAdminSubscriptionsView._grants_list_response` - 6 stmts
- `_parse_duration_months` - 6 stmts
- `CeleryTaskStatusView.get` - 6 stmts
- `SiteAdminSubscriptionsView.post._parse_dollars` - 4 stmts
- `SiteAdminApiLimitsView.handle_no_permission` - 3 stmts
- `SiteAdminPluginsView.handle_no_permission` - 3 stmts
- `SiteAdminUsersView.handle_no_permission` - 3 stmts
- `SiteAdminHomeView.handle_no_permission` - 3 stmts

### `external_api/views.py` - 12 callables, 103 statements

- `TripActivityDetailView.patch` - 17 stmts **(write)**
- `TripCalendarSyncView.post` - 14 stmts **(write)**
- `SafetyCheckinPhotosView.post` - 13 stmts **(write)**
- `VisitSuggestionActionView.post` - 11 stmts **(write)**
- `SafetyCheckinPhotoDetailView.delete` - 9 stmts **(write)**
- `TripRsvpView.put` - 9 stmts **(write)**
- `SafetyCheckinMapDetailView.delete` - 8 stmts **(write)**
- `ProfileNoteDetailView.patch` - 8 stmts **(write)**
- `SafetyContactDefaultsView.put` - 7 stmts **(write)**
- `SafetyCheckinPhotosView.get` - 5 stmts
- `_resolve_own_pin` - 1 stmts
- `_has_place_fields` - 1 stmts

### `controllers/albums.py` - 9 callables, 91 statements

- `AlbumEditView.post` - 31 stmts **(write)**
- `AlbumAddPhotosView._add_external` - 23 stmts
- `AlbumAddPhotosView.post` - 13 stmts **(write)**
- `AlbumReorderView.post` - 7 stmts **(write)**
- `_parse_body` - 4 stmts
- `AlbumDeleteView.post` - 4 stmts **(write)**
- `_int_ids` - 3 stmts
- `AlbumDetailView.get` - 3 stmts
- `AlbumRemovePhotosView.post` - 3 stmts **(write)**

### `controllers/account.py` - 8 callables, 85 statements

- `CustomLoginView.form_invalid` - 16 stmts
- `_record_failed_attempt` - 14 stmts
- `ResendVerificationView.post` - 13 stmts **(write)**
- `_send_verification_email` - 12 stmts
- `E2EEPasswordResetConfirmView.form_valid` - 11 stmts
- `LoginTwoFactorOptionsView.post` - 9 stmts **(write)**
- `_raw_lockout_key` - 5 stmts
- `E2EEPasswordResetConfirmView.get_context_data` - 5 stmts

### `controllers/safety.py` - 17 callables, 85 statements

- `SafetyCheckinMessageView._resolve` - 10 stmts
- `SafetyCheckinMessageView.post` - 9 stmts **(write)**
- `SafetyImageView.post` - 8 stmts **(write)**
- `SafetyCheckinPartnerInviteAcceptView.post` - 6 stmts **(write)**
- `SafetyCheckinPartnerInviteDeclineView.post` - 6 stmts **(write)**
- `SafetyCheckinPartnerMarkSafeView.post` - 6 stmts **(write)**
- `SafetyContactOptOutView.post` - 6 stmts **(write)**
- `SafetyCheckinCancelView.post` - 5 stmts **(write)**
- `SafetyCheckinPartnerRemoveView.post` - 5 stmts **(write)**
- `SafetyCheckinMapDetachView.post` - 4 stmts **(write)**
- `SafetyImageView.delete` - 4 stmts **(write)**
- `SafetyContactPortalView.get` - 4 stmts
- `SafetyContactOptOutView.get` - 4 stmts
- `SafetyImageView._get_image` - 3 stmts
- `SafetyContactMarkSafeView.post` - 3 stmts **(write)**
- `_render_partner_picker` - 1 stmts
- `_render_attached_maps` - 1 stmts

### `controllers/tools.py` - 7 callables, 85 statements

- `ImportStartView.post` - 26 stmts **(write)**
- `ExportDownloadView.get` - 23 stmts
- `ExportStatusView.get` - 17 stmts
- `ImportStatusView.get` - 16 stmts
- `_import_dir` - 1 stmts
- `_export_error_partial` - 1 stmts
- `_import_error_partial` - 1 stmts

### `controllers/group_chats.py` - 7 callables, 71 statements

- `GroupSharePinRespondView.post` - 20 stmts **(write)**
- `GroupAddMembersView.post` - 15 stmts **(write)**
- `GroupSharePinView.post` - 12 stmts **(write)**
- `GroupMessageDeleteView.post` - 9 stmts **(write)**
- `GroupMuteToggleView.post` - 6 stmts **(write)**
- `GroupReadView.post` - 5 stmts **(write)**
- `GroupSharePinView.get` - 4 stmts

### `controllers/pin.py` - 4 callables, 67 statements

- `PinController.upload_takeout` - 39 stmts
- `PinController.import_confirmed` - 17 stmts
- `PinController._pending_media` - 9 stmts
- `PinCrisExtractedImageView.get` - 2 stmts

### `controllers/trivia.py` - 9 callables, 67 statements

- `TriviaBeginView.post` - 12 stmts **(write)**
- `TriviaInviteView.post` - 11 stmts **(write)**
- `TriviaKickParticipantView.post` - 11 stmts **(write)**
- `TriviaJoinView.post` - 7 stmts **(write)**
- `TriviaEndSessionView.post` - 7 stmts **(write)**
- `TriviaLeaveSessionView.post` - 7 stmts **(write)**
- `TriviaSettingsView.post` - 5 stmts **(write)**
- `TriviaChatHistoryView.get` - 4 stmts
- `TriviaLobbyView.get` - 3 stmts

### `controllers/calendar_sync.py` - 4 callables, 48 statements

- `CalendarImportView.post` - 30 stmts **(write)**
- `GoogleCalendarDisconnectView.post` - 9 stmts **(write)**
- `GoogleCalendarSettingsDisconnectView.post` - 8 stmts **(write)**
- `_drop_expired_account` - 1 stmts

### `controllers/detail_pins.py` - 2 callables, 42 statements

- `LocationWikiDetailPinEditView.post` - 34 stmts **(write)**
- `DetailPinEditView.delete` - 8 stmts **(write)**

### `controllers/direct_message_shares.py` - 5 callables, 36 statements

- `MessageSharePinView.post` - 12 stmts **(write)**
- `MessageShareFriendView.post` - 12 stmts **(write)**
- `MessageSharePinView.get` - 4 stmts
- `MessageShareTripView.get` - 4 stmts
- `MessageShareFriendView.get` - 4 stmts

### `controllers/direct_messages.py` - 3 callables, 36 statements

- `MessageImagePermissionView.post` - 16 stmts **(write)**
- `MessageDeleteView.post` - 14 stmts **(write)**
- `ConversationReadView.post` - 6 stmts **(write)**

### `controllers/trip.py` - 3 callables, 33 statements

- `TripMemberOrganizerView.post` - 16 stmts **(write)**
- `TripCommentDeleteView.delete` - 10 stmts **(write)**
- `TripChildTripSearchView.get` - 7 stmts

### `controllers/google_photos.py` - 2 callables, 32 statements

- `PinGooglePhotosThumbnailView.get` - 21 stmts
- `PinGooglePhotosImportProgressView.get` - 11 stmts

### `controllers/visit_suggestions.py` - 1 callables, 31 statements

- `VisitSuggestionRespondView.post` - 31 stmts **(write)**

### `controllers/media_preview.py` - 1 callables, 30 statements

- `_fetch_source` - 30 stmts

### `controllers/image_gallery.py` - 6 callables, 28 statements

- `WikiImageView.post` - 10 stmts **(write)**
- `PinImageView.delete` - 7 stmts **(write)**
- `WikiImageView.delete` - 6 stmts **(write)**
- `store_uploaded_photo` - 2 stmts
- `WikiImageView._get_image` - 2 stmts
- `_wiki_for_location` - 1 stmts

### `controllers/markup.py` - 4 callables, 28 statements

- `MarkupEditView.delete` - 9 stmts **(write)**
- `_apply_security_indicator` - 7 stmts
- `_sanitize_text_box_corner` - 6 stmts
- `SafetyContactMarkupJsonView.get` - 6 stmts

### `controllers/photos.py` - 6 callables, 27 statements

- `PhotoActionView.log_visit` - 11 stmts
- `PhotoActionView.accept` - 6 stmts
- `PhotoActionView.reject` - 4 stmts
- `PhotoActionView.delete_photo` - 3 stmts
- `PhotoActionView.dismiss` - 2 stmts
- `PhotoActionView._pending_suggestion` - 1 stmts

### `controllers/spotguessr.py` - 4 callables, 27 statements

- `SpotGuessrInviteView.post` - 11 stmts **(write)**
- `SpotGuessrRoundView.get` - 9 stmts
- `SpotGuessrChatHistoryView.get` - 4 stmts
- `SpotGuessrLobbyView.get` - 3 stmts

### `controllers/flickr.py` - 4 callables, 25 statements

- `PinFlickrImportProgressView.get` - 11 stmts
- `_album_progress_response` - 10 stmts
- `PinFlickrAlbumImportProgressView.get` - 2 stmts
- `WikiFlickrAlbumImportProgressView.get` - 2 stmts

### `controllers/location_wiki.py` - 2 callables, 24 statements

- `LocationWikiRevertView.post` - 13 stmts **(write)**
- `WikiStatVoteView.post` - 11 stmts **(write)**

### `controllers/pin_lists.py` - 3 callables, 22 statements

- `PinListCreateView.post` - 15 stmts **(write)**
- `PinListRemoveItemView.post` - 4 stmts **(write)**
- `_default_trip_name_for_list` - 3 stmts

### `controllers/memories.py` - 1 callables, 15 statements

- `_attachment_label_url` - 15 stmts

### `controllers/immich.py` - 1 callables, 11 statements

- `PinImmichImportProgressView.get` - 11 stmts

### `controllers/notifications.py` - 2 callables, 11 statements

- `NotificationMarkReadView.post` - 7 stmts **(write)**
- `NotificationMarkAllReadView.post` - 4 stmts **(write)**

### `controllers/comments.py` - 1 callables, 7 statements

- `attach_existing_comment_image` - 7 stmts

### `controllers/pin_edit.py` - 1 callables, 7 statements

- `PinNoteDeleteView.delete` - 7 stmts **(write)**

### `controllers/settings.py` - 1 callables, 6 statements

- `SaveMapDarkModeView.post` - 6 stmts **(write)**

### `controllers/friendship.py` - 4 callables, 4 statements

- `FriendController.accept_friend` - 1 stmts
- `FriendController.reject_friend` - 1 stmts
- `FriendController.ignore_friend` - 1 stmts
- `FriendController.block_friend` - 1 stmts

### `controllers/maps.py` - 1 callables, 4 statements

- `_expand_state_codes` - 4 stmts

### `controllers/map_sharing.py` - 1 callables, 3 statements

- `MarkupMapShareDetailView.get` - 3 stmts

### `controllers/games.py` - 1 callables, 1 statements

- `RatedRow.rating` - 1 stmts

### `controllers/index.py` - 1 callables, 1 statements

- `IndexController.page_not_found` - 1 stmts

### `external_api/serializers.py` - 1 callables, 1 statements

- `SafetyPhotoSerializer.get_url` - 1 stmts

### `external_api/views_wiki.py` - 1 callables, 1 statements

- `_gallery_row` - 1 stmts

## What this list is not

Coverage measures execution, not correctness - a covered handler can still be wrong, and an
uncovered one can be perfectly fine. It is also scoped to two packages, so a service function
called by these handlers may well be tested even where the handler is not.

The reasonable order of work is the **write** handlers, largest first, since those are the ones
where an untested branch loses or corrupts user data rather than rendering badly.
