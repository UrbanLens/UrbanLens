<script>
  let map;
  
  import { onMount } from 'svelte';

  onMount(() => {
    const mapOptions = {
      center: { lat: 40.7128, lng: -74.0060 }, // New York by default
      zoom: 12,
    };
    map = new google.maps.Map(document.getElementById("map"), mapOptions);
    fetchPins();
  });

  async function fetchPins() {
    const response = await fetch('/api/pins/');
    const data = await response.json();
    data.forEach(pin => {
      new google.maps.Marker({
        position: { lat: pin.latitude, lng: pin.longitude },
        map,
      });
    });
  }
</script>

<div id="map" style="height: 500px;"></div>