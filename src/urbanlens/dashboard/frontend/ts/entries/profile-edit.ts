/** The profile edit page (``pages/profile/edit.html``). */

import { ProfileEditForm } from "../shared/profile-edit";

const root = document.querySelector<HTMLElement>(".edit-profile-page");
if (root) new ProfileEditForm(root).install();
