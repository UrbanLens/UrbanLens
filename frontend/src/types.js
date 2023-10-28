/*******************************************************************************
 *                                                                             *
 * Metadata:                                                                   *
 *                                                                             *
 * 	File: types.js                                                             *
 * 	Project: rita                                                              *
 * 	Created: 02 Oct 2023                                                       *
 * 	Author: Jess Mann                                                          *
 * 	Email: jess.a.mann@gmail.com                                               *
 *                                                                             *
 * 	-----                                                                      *
 *                                                                             *
 * 	Last Modified: Mon Oct 02 2023                                             *
 * 	Modified By: Jess Mann                                                     *
 *                                                                             *
 * 	-----                                                                      *
 *                                                                             *
 * 	Copyright (c) 2023 Jess Mann                                               *
 *******************************************************************************/
/**
 * @typedef {Object} RitaModel
 * @property {number} id
 */

/**
 * @typedef {RitaModel & Object} PersonModel
 * @property {string} name
 */

/**
 * @typedef {RitaModel & Object} DocumentModel
 * @property {string} title
 */

/**
 * @typedef {RitaModel & Object} CaseModel
 * @property {string} title
 */

export { };