<script>
  import { onMount } from 'svelte';
  import Map from './map.svelte';
  import Modals from './modals.svelte';
  import { getUserCoordinates } from './utilities.js';

  /**
   * @type {import('mapbox-gl').LngLatLike}
   */
  let userCoordinates;

  /**
   * return {Promise<void>}
   */
  async function submitLocation() {
    // Your fetch logic here
    console.log('submitLocation');
  }

  /**
   * @param {string} category
   * @param {string} name
   * @param {number} priority
   * @param {number} latitude
   * @param {number} longitude
   * @return {Promise<void>}
   */
  async function filterLocations(category, name, priority, latitude, longitude) {
    // Your fetch logic here
    console.log('filterLocations');
  }

  /**
   * return {Promise<void>}
   */
  function addLocation() {
    // Your logic here
    console.log('addLocation');
  }

  onMount(async () => {
    try {
      userCoordinates = await getUserCoordinates();
    } catch (error) {
      console.error('Failed to get user coordinates:', error);
    }
  });
</script>

<Map {userCoordinates} />
<Modals {submitLocation} {filterLocations} {addLocation} />