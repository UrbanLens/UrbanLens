/**
 * The photo gallery card (`partials/pins/_photo_gallery.html`), which loads this itself: a module script runs
 * once per page however often htmx swaps the card in.
 */
import { installPhotoGallery } from "../shared/photo-gallery";

installPhotoGallery();
