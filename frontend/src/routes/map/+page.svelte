<style>
  .map {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 100%;
    height: calc(100vh - 60px); /* Adjust based on the height of your header and footer */
  }
  .sidebar {
    background-color: rgba(35, 55, 75, 0.7); /* Make the sidebar slightly opaque */
    color: #fff;
    padding: 6px 12px;
    font-family: monospace;
    z-index: 1;
    position: absolute;
    top: 10px; /* Position the sidebar at the top left of the map */
    left: 10px;
    border-radius: 4px;
  }
</style>

<script>
  import { onMount, onDestroy } from 'svelte';
  import mapbox from 'mapbox-gl';
	import "../../../node_modules/mapbox-gl/dist/mapbox-gl.css"

  /**
   * @type {mapbox.Map}
   */
  let map;
  /**
   * @type {HTMLDivElement}
   */
  let mapContainer;
  let lat = 42.65698624597273;
  let lng = -73.75144231302086;
  let zoom = 9;

  function updateData() {
    	zoom = map.getZoom();
    	lng = map.getCenter().lng;
    	lat = map.getCenter().lat;
  }

	onMount(async () => {
		const initialState = { lng: lng, lat: lat, zoom: zoom };

		map = new mapbox.Map({
			container: mapContainer,
			accessToken: import.meta.env.VITE_MAPBOX_API_TOKEN,
			style: `mapbox://styles/mapbox/outdoors-v11`,
			center: [initialState.lng, initialState.lat],
			zoom: initialState.zoom,
		});

		map.on('move', () => {
			updateData();
		})

	onDestroy(() => {
		if (map) {
      map.remove();
    }
  });

  let showModal = false;
  let filterModal = false;
  let locationName = '';
  let locationIcon = '';
  let locationDescription = '';
  let locationCategories = '';
  let locationPriority = 0;
  let locationLastVisited = '';
  let locationLatitude = 0;
  let locationLongitude = 0;
  let locationProfile = '';
  let locationPinIcon = '';
  let selectedCategory = '';
  let categories = [];

  async function submitLocation() {
    const response = await fetch('/api/locations', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${user.token}`
      },
      body: JSON.stringify({ name: locationName, description: locationDescription, lat, lng })
    });

    if (response.ok) {
      showModal = false;
    } else {
      console.error('Failed to add location');
    }
  }

  async function filterLocations(category) {
    const response = await fetch(`/api/locations?category=${category}`, {
      headers: {
        'Authorization': `Bearer ${user.token}`
      },
    });

    if (response.ok) {
      const locations = await response.json();
      // TODO: Update the map with the filtered locations
    } else {
      console.error('Failed to filter locations');
    }
  }

  function addLocation() {
    showModal = true;
  }
});
</script>

<div>
  <div class="sidebar">
    Longitude: {lng.toFixed(4)} | Latitude: {lat.toFixed(4)} | Zoom: {zoom.toFixed(2)}
    <button on:click={() => filterModal = true}>Filter</button>
  </div>

  <div class="map-wrap">
    <div class="map" bind:this={mapContainer} on:contextmenu|preventDefault={showContextMenu} />
    {#if contextMenuVisible}
      <div class="context-menu" style="top: {contextMenuY}px; left: {contextMenuX}px;">
        <ul>
          <li on:click={addLocation}>Add a location</li>
        </ul>
      </div>
    {/if}
    {#if showModal}
      <div class="modal">
        <h2>Add a location</h2>
        <input type="text" bind:value={locationName} placeholder="Location name" />
        <input type="text" bind:value={locationIcon} placeholder="Location icon" />
        <input type="text" bind:value={locationDescription} placeholder="Location description" />
        <input type="text" bind:value={locationCategories} placeholder="Location categories" />
        <input type="number" bind:value={locationPriority} placeholder="Location priority" />
        <input type="date" bind:value={locationLastVisited} placeholder="Last visited" />
        <input type="number" bind:value={locationLatitude} placeholder="Latitude" />
        <input type="number" bind:value={locationLongitude} placeholder="Longitude" />
        <input type="text" bind:value={locationProfile} placeholder="Profile" />
        <input type="file" bind:value={locationPinIcon} placeholder="Pin icon" />
        <button on:click={submitLocation}>Submit</button>
      </div>
    {/if}
    {#if filterModal}
      <div class="modal">
        <h2>Filter locations</h2>
        <select bind:value={selectedCategory}>
          <option value="">-- Select a category --</option>
          {#each categories as category}
            <option value={category.id}>{category.name}</option>
          {/each}
        </select>
        <button on:click={() => filterLocations(selectedCategory)}>Apply filter</button>
        <button on:click={() => filterModal = false}>Close</button>
      </div>
    {/if}
  </div>
</div>
