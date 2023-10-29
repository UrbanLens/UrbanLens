export const DEFAULT_LATITUDE = 42.65698624597273;
export const DEFAULT_LONGITUDE = -73.75144231302086;

export function getUserCoordinates() {
  return new Promise((resolve, reject) => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition((position) => {
        resolve({
          lat: position.coords.latitude,
          lng: position.coords.longitude,
        });
      }, () => {
        resolve({
          lat: DEFAULT_LATITUDE,
          lng: DEFAULT_LONGITUDE,
        });
      });
    } else {
      resolve({
        lat: DEFAULT_LATITUDE,
        lng: DEFAULT_LONGITUDE,
      });
    }
  });
}
