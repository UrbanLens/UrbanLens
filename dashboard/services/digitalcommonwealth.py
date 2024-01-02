"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    digitalcommonwealth.py                                                                               *
*        Path:    /dashboard/services/digitalcommonwealth.py                                                           *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from dashboard.services.gateway import Gateway
import requests

class DigitalCommonwealthGateway(Gateway):
    def __init__(self):
        self.session = requests.Session()

    def get_oai_metadata(self, identifier):
        base_url = 'https://oai.digitalcommonwealth.org/catalog/oai'
        params = {
            'verb': 'GetRecord',
            'metadataPrefix': 'oai_dc',
            'identifier': identifier
        }
        response = self.session.get(base_url, params=params)
        response.raise_for_status()
        return response.content

    def search_items_json(self, query):
        search_url = f'https://www.digitalcommonwealth.org/search.json?q={query}'
        response = self.session.get(search_url)
        response.raise_for_status()
        return response.json()

    def get_item_details_json(self, item_id):
        details_url = f'https://www.digitalcommonwealth.org/search/commonwealth:{item_id}.json'
        response = self.session.get(details_url)
        response.raise_for_status()
        return response.json()

    def get_iiif_image_info(self, image_id):
        info_url = f'https://iiif.digitalcommonwealth.org/iiif/2/{image_id}/info.json'
        response = self.session.get(info_url)
        response.raise_for_status()
        return response.json()

    def get_iiif_manifest(self, item_id):
        manifest_url = f'https://www.digitalcommonwealth.org/search/commonwealth:{item_id}/manifest'
        response = self.session.get(manifest_url)
        response.raise_for_status()
        return response.json()
