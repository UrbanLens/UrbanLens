"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    queryset.pyi                                                                                         *
*        Path:    /queryset.pyi                                                                                        *
*        Project: profile                                                                                              *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from urbanlens.dashboard.models.abstract import QuerySet as AbstractQuerySet, Manager as AbstractManager

class QuerySet(AbstractQuerySet):
    ...

class Manager(AbstractManager, QuerySet):
    def all(self) -> QuerySet: ...
