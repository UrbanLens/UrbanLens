<script>
  import { onMount } from 'svelte';
  import { Map, Marker } from 'svelte-google-maps';

  let map;
  let center = { lat: 40.730610, lng: -73.935242 }; // New York coordinates

  onMount(async () => {
    map = new google.maps.Map(document.getElementById('map'), {
      center,
      zoom: 12
    });

    new google.maps.Marker({
      position: center,
      map: map
    });
  });
</script>

<div class="section no-pad-bot" id="index-banner">
  <div class="container">
    <h1 class="header center orange-text">Map</h1>
  </div>
</div>

<div class="container">
  <div id="map" style="height: 500px;"></div>
</div>
