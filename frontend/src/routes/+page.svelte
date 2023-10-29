<script>
  import { Button, Typography, LayoutGrid } from 'svelte-material-ui';
</script>

<LayoutGrid style="height: 100vh; width: 100%;">
  <LayoutGrid.Inner>
    <LayoutGrid.Cell span="12">
      <Typography variant="headline4" class="center-text">Urban Lens</Typography>
    </LayoutGrid.Cell>
    <LayoutGrid.Cell span="6">
      <Button component="a" href="/account" raised>Account</Button>
    </LayoutGrid.Cell>
    <LayoutGrid.Cell span="6">
      <Button component="a" href="/map" raised>Map</Button>
    </LayoutGrid.Cell>
  </LayoutGrid.Inner>
</LayoutGrid>
